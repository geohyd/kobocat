# coding: utf-8
from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from datetime import date, datetime
from xml.etree import ElementTree as ET
from xml.parsers.expat import ExpatError
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo


from dict2xml import dict2xml
from django.conf import settings
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.files.storage import default_storage
from django.core.mail import mail_admins
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import (
    HttpResponse,
    HttpResponseNotFound,
    StreamingHttpResponse,
    Http404
)
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.encoding import DjangoUnicodeDecodeError, smart_str
from django.utils.translation import gettext as t
from kobo_service_account.utils import get_real_user
from modilabs.utils.subprocess_timeout import ProcessTimedOut
from pyxform.errors import PyXFormError
from pyxform.xform2json import create_survey_element_from_xml
from xml.dom import Node
from wsgiref.util import FileWrapper

from onadata.apps.logger.exceptions import (
    DuplicateUUIDError,
    FormInactiveError,
    TemporarilyUnavailableError,
)
from onadata.apps.logger.models import Attachment, Instance, XForm
from onadata.apps.logger.models.attachment import (
    generate_attachment_filename,
    hash_attachment_contents,
)
from onadata.apps.logger.models.instance import (
    InstanceHistory,
    get_id_string_from_xml_str,
    update_xform_daily_counter,
    update_xform_monthly_counter,
    update_xform_submission_count,
)
from onadata.apps.logger.models.xform import XLSFormError
from onadata.apps.logger.signals import (
    post_save_attachment,
    pre_delete_attachment,
)

from onadata.apps.logger.xform_instance_parser import (
    InstanceEmptyError,
    InstanceInvalidUserError,
    InstanceMultipleNodeError,
    DuplicateInstance,
    clean_and_parse_xml,
    get_uuid_from_xml,
    get_deprecated_uuid_from_xml,
    get_submission_date_from_xml,
    get_xform_media_question_xpaths,
)
from onadata.apps.main.models import UserProfile
from onadata.apps.viewer.models.data_dictionary import DataDictionary
from onadata.apps.viewer.models.parsed_instance import ParsedInstance
from onadata.libs.utils import common_tags
from onadata.libs.utils.model_tools import queryset_iterator, set_uuid

OPEN_ROSA_VERSION_HEADER = 'X-OpenRosa-Version'
HTTP_OPEN_ROSA_VERSION_HEADER = 'HTTP_X_OPENROSA_VERSION'
OPEN_ROSA_VERSION = '1.0'
DEFAULT_CONTENT_TYPE = 'text/xml; charset=utf-8'
DEFAULT_CONTENT_LENGTH = settings.DEFAULT_CONTENT_LENGTH

uuid_regex = re.compile(r'<formhub>\s*<uuid>\s*([^<]+)\s*</uuid>\s*</formhub>',
                        re.DOTALL)

mongo_instances = settings.MONGO_DB.instances


def check_submission_permissions(
    request: 'rest_framework.request.Request', xform: XForm
):
    """
    Check that permission is required and the request user has permission.

    The user does no have permissions iff:
        * the user is authed,
        * either the profile or the form require auth,
        * the xform user is not submitting.

    Since we have a username, the Instance creation logic will
    handle checking for the forms existence by its id_string.

    :returns: None.
    :raises: PermissionDenied based on the above criteria.
    """
    profile = UserProfile.objects.get_or_create(user=xform.user)[0]
    if (
        request
        and (
            profile.require_auth
            or xform.require_auth
            or request.path == '/submission'
        )
        and xform.user != request.user
        and not request.user.has_perm('report_xform', xform)
    ):
        raise PermissionDenied(t('Forbidden'))


def check_edit_submission_permissions(
    request: 'rest_framework.request.Request', xform: XForm
):
    if request.user.is_anonymous:
        raise UnauthenticatedEditAttempt
    if not _has_edit_xform_permission(request, xform):
        raise PermissionDenied(t(
            'Forbidden attempt to edit a submission. To make a new submission, '
            'Remove `deprecatedID` from the submission XML and try again.'
        ))


@transaction.atomic  # paranoia; redundant since `ATOMIC_REQUESTS` set to `True`
def create_instance(
    username: str,
    xml_file: str,
    media_files: list['django.core.files.uploadedfile.UploadedFile'],
    status: str = 'submitted_via_web',
    uuid: str = None,
    date_created_override: datetime = None,
    request: 'rest_framework.request.Request' = None,
) -> Instance:
    """
    Submission cases:
        If there is a username and no uuid, submitting an old ODK form.
        If there is a username and a uuid, submitting a new ODK form.
    """

    if username:
        username = username.lower()

    xml = smart_str(xml_file.read())
    xml_hash = Instance.get_hash(xml)
    xform = get_xform_from_submission(xml, username, uuid)
    check_submission_permissions(request, xform)

    # get new and deprecated uuid's
    new_uuid = get_uuid_from_xml(xml)

    # Dorey's rule from 2012 (commit 890a67aa):
    #   Ignore submission as a duplicate IFF
    #    * a submission's XForm collects start time
    #    * the submitted XML is an exact match with one that
    #      has already been submitted for that user.
    # The start-time requirement protected submissions with identical responses
    # from being rejected as duplicates *before* KoBoCAT had the concept of
    # submission UUIDs. Nowadays, OpenRosa requires clients to send a UUID (in
    # `<instanceID>`) within every submission; if the incoming XML has a UUID
    # and still exactly matches an existing submission, it's certainly a
    # duplicate (https://docs.opendatakit.org/openrosa-metadata/#fields).
    if xform.has_start_time or new_uuid is not None:
        # XML matches are identified by identical content hash OR, when a
        # content hash is not present, by string comparison of the full
        # content, which is slow! Use the management command
        # `populate_xml_hashes_for_instances` to hash existing submissions
        existing_instance = Instance.objects.filter(
            Q(xml_hash=xml_hash) | Q(xml_hash=Instance.DEFAULT_XML_HASH, xml=xml),
            xform__user=xform.user,
        ).first()
    else:
        existing_instance = None

    if existing_instance:
        existing_instance.check_active(force=False)
        # ensure we have saved the extra attachments
        new_attachments, _ = save_attachments(existing_instance, media_files)
        if not new_attachments:
            raise DuplicateInstance()
        else:
            # Update Mongo via the related ParsedInstance
            existing_instance.parsed_instance.save(asynchronous=False)
            return existing_instance
    else:
        instance = save_submission(request, xform, xml, media_files, new_uuid,
                                   status, date_created_override)
        return instance


def disposition_ext_and_date(name, extension, show_date=True):
    if name is None:
        return 'attachment;'
    if show_date:
        name = "%s_%s" % (name, date.today().strftime("%Y_%m_%d"))
    return 'attachment; filename=%s.%s' % (name, extension)


def dict2xform(jsform, form_id):
    dd = {'form_id': form_id}
    xml_head = "<?xml version='1.0' ?>\n<%(form_id)s id='%(form_id)s'>\n" % dd
    xml_tail = "\n</%(form_id)s>" % dd

    return xml_head + dict2xml(jsform) + xml_tail


def get_instance_or_404(**criteria):
    """
    Mimic `get_object_or_404` but handles duplicate records.

    `logger_instance` can contain records with the same `uuid`

    :param criteria: dict
    :return: Instance
    """
    instances = Instance.objects.filter(**criteria).order_by("id")
    if instances:
        instance = instances[0]
        xml_hash = instance.xml_hash
        for instance_ in instances[1:]:
            if instance_.xml_hash == xml_hash:
                continue
            raise DuplicateUUIDError(
                "Multiple instances with different content exist for UUID "
                "{}".format(instance.uuid)
            )

        return instance
    else:
        raise Http404


def get_uuid_from_submission(xml):
    # parse UUID from uploaded XML
    split_xml = uuid_regex.split(xml)

    # check that xml has UUID
    return len(split_xml) > 1 and split_xml[1] or None


def get_xform_from_submission(xml, username, uuid=None):
    # check alternative form submission ids
    uuid = uuid or get_uuid_from_submission(xml)

    if not username and not uuid:
        raise InstanceInvalidUserError()

    if uuid:
        # try to find the form by its uuid which is the ideal condition
        try:
            xform = XForm.objects.get(uuid=uuid)
        except XForm.DoesNotExist:
            pass
        else:
            return xform

    id_string = get_id_string_from_xml_str(xml)

    return get_object_or_404(
        XForm, id_string__exact=id_string, user__username=username
    )


def inject_instanceid(xml_str, uuid):
    if get_uuid_from_xml(xml_str) is None:
        xml = clean_and_parse_xml(xml_str)
        children = xml.childNodes
        if children.length == 0:
            raise ValueError(t("XML string must have a survey element."))

        # check if we have a meta tag
        survey_node = children.item(0)
        meta_tags = [
            n for n in survey_node.childNodes
            if n.nodeType == Node.ELEMENT_NODE
            and n.tagName.lower() == "meta"]
        if len(meta_tags) == 0:
            meta_tag = xml.createElement("meta")
            xml.documentElement.appendChild(meta_tag)
        else:
            meta_tag = meta_tags[0]

        # check if we have an instanceID tag
        uuid_tags = [
            n for n in meta_tag.childNodes
            if n.nodeType == Node.ELEMENT_NODE
            and n.tagName == "instanceID"]
        if len(uuid_tags) == 0:
            uuid_tag = xml.createElement("instanceID")
            meta_tag.appendChild(uuid_tag)
        else:
            uuid_tag = uuid_tags[0]
        # insert meta and instanceID
        text_node = xml.createTextNode("uuid:%s" % uuid)
        uuid_tag.appendChild(text_node)
        return xml.toxml()
    return xml_str


def mongo_sync_status(remongo=False, update_all=False, user=None, xform=None):
    """
    Check the status of records in the PostgreSQL db versus MongoDB.
    At a minimum, return a report (string) of the results.

    Optionally, take action to correct the differences, based on these
    parameters, if present and defined:

    remongo    -> if True, update the records missing in mongodb
                  (default: False)
    update_all -> if True, update all the relevant records (default: False)
    user       -> if specified, apply only to the forms for the given user
                  (default: None)
    xform      -> if specified, apply only to the given form (default: None)

    """

    qs = XForm.objects.only('id_string', 'user').select_related('user')
    if user and not xform:
        qs = qs.filter(user=user)
    elif user and xform:
        qs = qs.filter(user=user, id_string=xform.id_string)
    else:
        qs = qs.all()

    total = qs.count()
    found = 0
    done = 0
    total_to_remongo = 0
    report_string = ""
    for xform in queryset_iterator(qs, 100):
        # get the count
        user = xform.user
        instance_count = Instance.objects.filter(xform=xform).count()
        userform_id = "%s_%s" % (user.username, xform.id_string)
        mongo_count = mongo_instances.count_documents(
            {common_tags.USERFORM_ID: userform_id},
            maxTimeMS=settings.MONGO_DB_MAX_TIME_MS
        )

        if instance_count != mongo_count or update_all:
            line = "user: %s, id_string: %s\nInstance count: %d\t" \
                   "Mongo count: %d\n---------------------------------" \
                   "-----\n" % (
                       user.username, xform.id_string, instance_count,
                       mongo_count)
            report_string += line
            found += 1
            total_to_remongo += (instance_count - mongo_count)

            # should we remongo
            if remongo or (remongo and update_all):
                if update_all:
                    sys.stdout.write(
                        "Updating all records for %s\n--------------------"
                        "---------------------------\n" % xform.id_string)
                else:
                    sys.stdout.write(
                        "Updating missing records for %s\n----------------"
                        "-------------------------------\n"
                        % xform.id_string)
                _update_mongo_for_xform(
                    xform, only_update_missing=not update_all
                )
        done += 1
        sys.stdout.write(
            "%.2f %% done ...\r" % ((float(done) / float(total)) * 100))
    # only show stats if we are not updating mongo, the update function
    # will show progress
    if not remongo:
        line = "Total # of forms out of sync: %d\n" \
               "Total # of records to remongo: %d\n" % (found, total_to_remongo)
        report_string += line
    return report_string


def publish_form(callback):
    try:
        return callback()
    except (PyXFormError, XLSFormError) as e:
        return {
            'type': 'alert-error',
            'text': str(e)
        }
    except IntegrityError as e:
        return {
            'type': 'alert-error',
            'text': str(e),
        }
    except ValidationError as e:
        # on clone invalid URL
        return {
            'type': 'alert-error',
            'text': t('Invalid URL format.'),
        }
    except AttributeError as e:
        # form.publish returned None, not sure why...
        return {
            'type': 'alert-error',
            'text': str(e)
        }
    except ProcessTimedOut as e:
        # catch timeout errors
        return {
            'type': 'alert-error',
            'text': t('Form validation timeout, please try again.'),
        }
    except Exception as e:
        # TODO: Something less horrible. This masks storage backend
        # `ImportError`s and who knows what else

        # ODK validation errors are vanilla errors and it masks a lot of regular
        # errors if we try to catch it so let's catch it, BUT reraise it
        # if we don't see typical ODK validation error messages in it.
        if "ODK Validate Errors" not in str(e):
            raise

        # error in the XLS file; show an error to the user
        return {
            'type': 'alert-error',
            'text': str(e)
        }


def publish_xls_form(xls_file, user, id_string=None):
    """
    Creates or updates a DataDictionary with supplied xls_file,
    user and optional id_string - if updating
    """
    # get or create DataDictionary based on user and id string
    if id_string:
        dd = DataDictionary.objects.get(user=user, id_string=id_string)
        dd.xls = xls_file
        dd.save()
        return dd
    else:
        # Creation needs to be wrapped in a transaction because of unit tests.
        # It raises `TransactionManagementError` on IntegrityError in
        # `RestrictedAccessMiddleware` when accessing `request.user.profile`.
        # See https://stackoverflow.com/a/23326971
        try:
            with transaction.atomic():
                dd = DataDictionary.objects.create(user=user, xls=xls_file)
        except IntegrityError as e:
            raise e
        return dd


def publish_xml_form(xml_file, user, id_string=None):
    xml = smart_str(xml_file.read())
    survey = create_survey_element_from_xml(xml)
    form_json = survey.to_json()
    if id_string:
        dd = DataDictionary.objects.get(user=user, id_string=id_string)
        dd.xml = xml
        dd.json = form_json
        dd._mark_start_time_boolean()
        set_uuid(dd)
        dd.set_uuid_in_xml()
        dd.save()
        return dd
    else:
        dd = DataDictionary(user=user, xml=xml, json=form_json)
        dd._mark_start_time_boolean()
        set_uuid(dd)
        dd.set_uuid_in_xml(file_name=xml_file.name)
        dd.save()
        return dd


def report_exception(subject, info, exc_info=None):
    # TODO: replace with standard logging (i.e. `import logging`)
    if exc_info:
        cls, err = exc_info[:2]
        message = t("Exception in request:"
                    " %(class)s: %(error)s")\
            % {'class': cls.__name__, 'error': err}
        message += "".join(traceback.format_exception(*exc_info))
    else:
        message = "%s" % info

    if settings.DEBUG or settings.TESTING_MODE:
        sys.stdout.write("Subject: %s\n" % subject)
        sys.stdout.write("Message: %s\n" % message)
    else:
        mail_admins(subject=subject, message=message)


def response_with_mimetype_and_name(
        mimetype, name, extension=None, show_date=True, file_path=None,
        use_local_filesystem=False, full_mime=False):
    if extension is None:
        extension = mimetype
    if not full_mime:
        mimetype = "application/%s" % mimetype
    if file_path:
        try:
            if not use_local_filesystem:
                wrapper = FileWrapper(default_storage.open(file_path))
                response = StreamingHttpResponse(wrapper, content_type=mimetype)
                response['Content-Length'] = default_storage.size(file_path)
            else:
                wrapper = FileWrapper(open(file_path))
                response = StreamingHttpResponse(wrapper, content_type=mimetype)
                response['Content-Length'] = os.path.getsize(file_path)
        except IOError:
            response = HttpResponseNotFound(
                t("The requested file could not be found."))
    else:
        response = HttpResponse(content_type=mimetype)
    response['Content-Disposition'] = disposition_ext_and_date(
        name, extension, show_date)
    return response


def safe_create_instance(username, xml_file, media_files, uuid, request):
    """Create an instance and catch exceptions.

    :returns: A list [error, instance] where error is None if there was no
        error.
    """
    error = instance = None

    try:
        instance = create_instance(
            username, xml_file, media_files, uuid=uuid, request=request)
    except InstanceInvalidUserError:
        error = OpenRosaResponseBadRequest(t("Username or ID required."))
    except InstanceEmptyError:
        error = OpenRosaResponseBadRequest(
            t("Received empty submission. No instance was created")
        )
    except FormInactiveError:
        error = OpenRosaResponseNotAllowed(t("Form is not active"))
    except TemporarilyUnavailableError:
        error = OpenRosaTemporarilyUnavailable(t("Temporarily unavailable"))
    except XForm.DoesNotExist:
        error = OpenRosaResponseNotFound(
            t("Form does not exist on this account")
        )
    except ExpatError as e:
        error = OpenRosaResponseBadRequest(t("Improperly formatted XML."))
    except DuplicateInstance:
        response = OpenRosaResponse(t("Duplicate submission"))
        response.status_code = 202
        response['Location'] = request.build_absolute_uri(request.path)
        error = response
    except PermissionDenied as e:
        error = OpenRosaResponseForbidden(e)
    except InstanceMultipleNodeError as e:
        error = OpenRosaResponseBadRequest(e)
    except DjangoUnicodeDecodeError:
        error = OpenRosaResponseBadRequest(t("File likely corrupted during "
                                             "transmission, please try later."
                                             ))

    return [error, instance]


def save_attachments(
    instance: Instance,
    media_files: list['django.core.files.uploadedfile.UploadedFile'],
    defer_counting: bool = False,
) -> tuple[list[Attachment], list[Attachment]]:
    """
    Return a tuple of two lists.
    - The former is new attachments
    - The latter is the replaced/soft-deleted attachments

    `defer_counting=False` will set a Python-only attribute of the same name on
    any *new* `Attachment` instances created. This will prevent
    `update_xform_attachment_storage_bytes()` and friends from doing anything,
    which avoids locking any rows in `logger_xform` or `main_userprofile`.
    """
    new_attachments = []
    for f in media_files:
        attachment_filename = generate_attachment_filename(instance, f.name)
        existing_attachment = Attachment.objects.filter(
            instance=instance,
            media_file=attachment_filename,
            mimetype=f.content_type,
        ).first()
        if existing_attachment and (existing_attachment.file_hash ==
                                    hash_attachment_contents(f.read())):
            # We already have this attachment!
            continue
        f.seek(0)
        # This is a new attachment; save it!
        new_attachment = Attachment(
            instance=instance, media_file=f, mimetype=f.content_type
        )
        if defer_counting:
            # Only set the attribute if requested, i.e. don't bother ever
            # setting it to `False`
            new_attachment.defer_counting = True
        new_attachment.save()
        new_attachments.append(new_attachment)

    soft_deleted_attachments = get_soft_deleted_attachments(instance)

    return new_attachments, soft_deleted_attachments


def save_submission(
    request: 'rest_framework.request.Request',
    xform: XForm,
    xml: str,
    media_files: list['django.core.files.uploadedfile.UploadedFile'],
    new_uuid: str,
    status: str,
    date_created_override: datetime,
) -> Instance:

    if not date_created_override:
        date_created_override = get_submission_date_from_xml(xml)

    # We have to save the `Instance` to the database before we can associate
    # any `Attachment`s with it, but we are inside a transaction and saving
    # attachments is slow! Usually creating an `Instance` updates the
    # submission count of the parent `XForm` automatically via a `post_save`
    # signal, but that takes a lock on `logger_xform` that persists until the
    # end of the transaction.  We must avoid doing that until all attachments
    # are saved, and we are as close as possible to the end of the transaction.
    # See https://github.com/kobotoolbox/kobocat/issues/490.
    #
    # `_get_instance(..., defer_counting=True)` skips incrementing the
    # submission counters and returns an `Instance` with a `defer_counting`
    # attribute set to `True` *if* a new instance was created. We are
    # responsible for calling `update_xform_submission_count()` if the returned
    # `Instance` has `defer_counting = True`.
    instance = _get_instance(
        request, xml, new_uuid, status, xform, defer_counting=True
    )

    new_attachments, soft_deleted_attachments = save_attachments(
        instance, media_files, defer_counting=True
    )

    # override date created if required
    if date_created_override:
        if not timezone.is_aware(date_created_override):
            # default to utc?
            date_created_override = timezone.make_aware(
                date_created_override, timezone.utc)
        instance.date_created = date_created_override
        instance.save()

    if instance.xform is not None:
        pi, created = ParsedInstance.objects.get_or_create(
            instance=instance)

    if not created:
        pi.save(asynchronous=False)

    # Now that the slow tasks are complete and we are (hopefully!) close to the
    # end of the transaction, update the submission count if the `Instance` was
    # newly created
    if getattr(instance, 'defer_counting', False):
        # Remove the Python-only attribute
        del instance.defer_counting
        update_xform_daily_counter(
            sender=Instance, instance=instance, created=True
        )
        update_xform_monthly_counter(
            sender=Instance, instance=instance, created=True
        )
        update_xform_submission_count(
            sender=Instance, instance=instance, created=True
        )

    # Update the storage totals for new attachments as well, which were
    # deferred for the same performance reasons
    for new_attachment in new_attachments:
        if getattr(new_attachment, 'defer_counting', False):
            # Remove the Python-only attribute
            del new_attachment.defer_counting
            post_save_attachment(new_attachment, created=True)

    for soft_deleted_attachment in soft_deleted_attachments:
        pre_delete_attachment(soft_deleted_attachment, only_update_counters=True)

    return instance


def get_soft_deleted_attachments(instance: Instance) -> list[Attachment]:
    """
    Soft delete replaced attachments when editing a submission
    """
    # Retrieve all media questions of Xform
    media_question_xpaths = get_xform_media_question_xpaths(instance.xform)

    # If XForm does not have any media fields, do not go further
    if not media_question_xpaths:
        return []

    # Parse instance XML to get the basename of each file of the updated
    # submission
    xml_parsed = ET.fromstring(instance.xml)
    basenames = []

    for media_question_xpath in media_question_xpaths:
        root_name, xpath_without_root = media_question_xpath.split('/', 1)
        try:
            assert root_name == xml_parsed.tag
        except AssertionError:
            logging.warning(
                'Instance XML root tag name does not match with its form'
            )

        # With repeat groups, several nodes can have the same XPath. We
        # need to retrieve all of them
        questions = xml_parsed.findall(xpath_without_root)
        for question in questions:
            try:
                basename = question.text
            except AttributeError:
                raise XPathNotFoundException

            # Only keep non-empty fields
            if basename:
                basenames.append(basename)

    # Update Attachment objects to hide them if they are not used anymore.
    # We do not want to delete them until the instance itself is deleted.
    queryset = Attachment.objects.filter(
        instance=instance
    ).exclude(media_file_basename__in=basenames)
    soft_deleted_attachments = list(queryset.all())
    queryset.update(deleted_at=timezone.now())

    return soft_deleted_attachments


def _get_instance(
    request: 'rest_framework.request.Request',
    xml: str,
    new_uuid: str,
    status: str,
    xform: XForm,
    defer_counting: bool = False,
) -> Instance:
    """
    `defer_counting=False` will set a Python-only attribute of the same name on
    the *new* `Instance` if one is created. This will prevent
    `update_xform_submission_count()` from doing anything, which avoids locking
    any rows in `logger_xform` or `main_userprofile`.
    """
    # check if its an edit submission
    old_uuid = get_deprecated_uuid_from_xml(xml)
    instances = Instance.objects.filter(uuid=old_uuid)

    if instances:
        # edits
        instance = instances[0]
        check_edit_submission_permissions(request, xform)
        InstanceHistory.objects.create(
            xml=instance.xml, xform_instance=instance, uuid=old_uuid)
        instance.xml = xml
        instance._populate_xml_hash()
        instance.uuid = new_uuid
        instance.save()
    else:
        submitted_by = (
            get_real_user(request)
            if request and request.user.is_authenticated
            else None
        )
        # new submission
        # Avoid `Instance.objects.create()` so that we can set a Python-only
        # attribute, `defer_counting`, before saving
        instance = Instance()
        instance.xml = xml
        instance.user = submitted_by
        instance.status = status
        instance.xform = xform
        if defer_counting:
            # Only set the attribute if requested, i.e. don't bother ever
            # setting it to `False`
            instance.defer_counting = True
        instance.save()

    return instance


def _has_edit_xform_permission(
    request: 'rest_framework.request.Request', xform: XForm
) -> bool:
    if isinstance(xform, XForm):
        if request.user.is_superuser:
            return True

        return request.user.has_perm('logger.change_xform', xform)

    return False


def _update_mongo_for_xform(xform, only_update_missing=True):

    instance_ids = set(
        [i.id for i in Instance.objects.only('id').filter(xform=xform)])
    sys.stdout.write("Total no of instances: %d\n" % len(instance_ids))
    mongo_ids = set()
    user = xform.user
    userform_id = "%s_%s" % (user.username, xform.id_string)
    if only_update_missing:
        sys.stdout.write("Only updating missing mongo instances\n")
        mongo_ids = set(
            [rec[common_tags.ID] for rec in mongo_instances.find(
                {common_tags.USERFORM_ID: userform_id},
                {common_tags.ID: 1},
                max_time_ms=settings.MONGO_DB_MAX_TIME_MS
        )])
        sys.stdout.write("Total no of mongo instances: %d\n" % len(mongo_ids))
        # get the difference
        instance_ids = instance_ids.difference(mongo_ids)
    else:
        # clear mongo records
        mongo_instances.delete_many({common_tags.USERFORM_ID: userform_id})

    # get instances
    sys.stdout.write(
        "Total no of instances to update: %d\n" % len(instance_ids))
    instances = Instance.objects.only('id').in_bulk(
        [id_ for id_ in instance_ids])
    total = len(instances)
    done = 0
    for id_, instance in instances.items():
        (pi, created) = ParsedInstance.objects.get_or_create(instance=instance)
        try:
            save_success = pi.save(asynchronous=False)
        except InstanceEmptyError:
            print(
                "\033[91m[WARNING] - Skipping Instance #{}/uuid:{} because "
                "it is empty\033[0m".format(id_, instance.uuid)
            )
        else:
            if not save_success:
                print(
                    "\033[91m[ERROR] - Instance #{}/uuid:{} - Could not save "
                    "the parsed instance\033[0m".format(id_, instance.uuid)
                )
            else:
                done += 1

        progress = "\r%.2f %% done..." % ((float(done) / float(total)) * 100)
        sys.stdout.write(progress)
        sys.stdout.flush()
    sys.stdout.write(
        "\nUpdated %s\n------------------------------------------\n"
        % xform.id_string)


class BaseOpenRosaResponse(HttpResponse):
    status_code = 201

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self[OPEN_ROSA_VERSION_HEADER] = OPEN_ROSA_VERSION
        dt = datetime.now(tz=ZoneInfo('UTC')).strftime('%a, %d %b %Y %H:%M:%S %Z')
        self['Date'] = dt
        self['X-OpenRosa-Accept-Content-Length'] = DEFAULT_CONTENT_LENGTH
        self['Content-Type'] = DEFAULT_CONTENT_TYPE


class OpenRosaResponse(BaseOpenRosaResponse):
    status_code = 201

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # wrap content around xml
        self.content = (
            b"<?xml version='1.0' encoding='UTF-8' ?>\n"
            b'<OpenRosaResponse xmlns="http://openrosa.org/http/response">\n'
            b'        <message nature="">'
        ) + self.content + (
            b'</message>\n'
            b'</OpenRosaResponse>'
        )


class OpenRosaResponseNotFound(OpenRosaResponse):
    status_code = 404


class OpenRosaResponseBadRequest(OpenRosaResponse):
    status_code = 400


class OpenRosaResponseNotAllowed(OpenRosaResponse):
    status_code = 405


class OpenRosaResponseForbidden(OpenRosaResponse):
    status_code = 403


class OpenRosaTemporarilyUnavailable(OpenRosaResponse):
    status_code = 503


class UnauthenticatedEditAttempt(Exception):
    """
    Escape hatch from the `safe_create_instance()` antipattern, where these
    "logger tools" return responses directly instead of raising exceptions.
    To avoid a large refactoring, this class allows the view code to handle
    returning the proper response to the client:
    `check_edit_submission_permissions()` raises `UnauthenticatedEditAttempt`,
    which passes through unmolested to `XFormSubmissionApi.create()`, which
    then returns the appropriate 401 response.
    """
    pass


class XPathNotFoundException(Exception):
    pass
