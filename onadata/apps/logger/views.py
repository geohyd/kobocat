# coding: utf-8
from datetime import datetime
import datetime as datetime_module
import json
import os
import tempfile
import re

import pytz
from django.contrib.auth.decorators import login_required, user_passes_test
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.contrib import messages
from django.core.files.storage import get_storage_class
from django.core.files import File
from django.http import (HttpResponse,
                         HttpResponseBadRequest,
                         HttpResponseForbidden,
                         HttpResponseRedirect,
                         StreamingHttpResponse,
                         Http404,
                         )
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template import loader
from django.utils.six import string_types, text_type
from django.utils.translation import ugettext as _
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django_digest import HttpDigestAuthenticator

from onadata.apps.main.models import UserProfile, MetaData
from onadata.apps.logger.import_tools import import_instances_from_zip
from onadata.apps.logger.models.attachment import Attachment
from onadata.apps.logger.models.instance import Instance
from onadata.apps.logger.models.xform import XForm
from onadata.libs.utils.log import audit_log, Actions
from onadata.libs.utils.logger_tools import (
    safe_create_instance,
    OpenRosaResponseBadRequest,
    OpenRosaResponse,
    BaseOpenRosaResponse,
    publish_xml_form,
    publish_form,
)
from onadata.libs.utils.logger_tools import response_with_mimetype_and_name
from onadata.libs.utils.user_auth import (helper_auth_helper,
                                          has_permission,
                                          HttpResponseNotAuthorized,
                                          add_cors_headers,
                                          )
from .tasks import generate_stats_zip
from ...koboform.pyxform_utils import convert_csv_to_xls

IO_ERROR_STRINGS = [
    'request data read error',
    'error during read(65536) on wsgi.input'
]


def _bad_request(e):
    strerror = text_type(e)

    return strerror and strerror in IO_ERROR_STRINGS


def _extract_uuid(text):
    text = text[text.find("@key="):-1].replace("@key=", "")
    if text.startswith("uuid:"):
        text = text.replace("uuid:", "")
    return text


def _parse_int(num):
    try:
        return num and int(num)
    except ValueError:
        pass


def _submission_response(request, instance):
    data = {
        'message': _("Successful submission."),
        'formid': instance.xform.id_string,
        'encrypted': instance.xform.encrypted,
        'instanceID': f'uuid:{instance.uuid}',
        'submissionDate': instance.date_created.isoformat(),
        'markedAsCompleteDate': instance.date_modified.isoformat()
    }

    #context = RequestContext(request, data)
    t = loader.get_template('submission.xml')

    return BaseOpenRosaResponse(t.render(data, request=request))


@require_POST
@csrf_exempt
def bulksubmission(request, username):
    # puts it in a temp directory.
    # runs "import_tools(temp_directory)"
    # deletes
    posting_user = get_object_or_404(User, username__iexact=username)

    # request.FILES is a django.utils.datastructures.MultiValueDict
    # for each key we have a list of values
    try:
        temp_postfile = request.FILES.pop("zip_submission_file", [])
    except IOError:
        return HttpResponseBadRequest(_("There was a problem receiving your "
                                        "ODK submission. [Error: IO Error "
                                        "reading data]"))
    if len(temp_postfile) != 1:
        return HttpResponseBadRequest(_("There was a problem receiving your"
                                        " ODK submission. [Error: multiple "
                                        "submission files (?)]"))

    postfile = temp_postfile[0]
    tempdir = tempfile.gettempdir()
    our_tfpath = os.path.join(tempdir, postfile.name)

    with open(our_tfpath, 'wb') as f:
        f.write(postfile.read())

    with open(our_tfpath, 'rb') as f:
        total_count, success_count, errors = import_instances_from_zip(
            f, posting_user)
    # chose the try approach as suggested by the link below
    # http://stackoverflow.com/questions/82831
    try:
        os.remove(our_tfpath)
    except IOError:
        # TODO: log this Exception somewhere
        pass
    json_msg = {
        'message': _("Submission complete. Out of %(total)d "
                     "survey instances, %(success)d were imported, "
                     "(%(rejected)d were rejected as duplicates, "
                     "missing forms, etc.)") %
        {'total': total_count, 'success': success_count,
         'rejected': total_count - success_count},
        'errors': "%d %s" % (len(errors), errors)
    }
    audit = {
        "bulk_submission_log": json_msg
    }
    audit_log(Actions.USER_BULK_SUBMISSION, request.user, posting_user,
              _("Made bulk submissions."), audit, request)
    response = HttpResponse(json.dumps(json_msg))
    response.status_code = 200
    response['Location'] = request.build_absolute_uri(request.path)
    return response


@login_required
def bulksubmission_form(request, username=None):
    username = username if username is None else username.lower()
    if request.user.username == username:
        return render(request, 'bulk_submission_form.html')
    else:
        return HttpResponseRedirect('/%s' % request.user.username)


@require_GET
def formList(request, username): # noqa
    """
    This is where ODK Collect gets its download list.
    """
    formlist_user = get_object_or_404(User, username__iexact=username)
    profile, created = UserProfile.objects.get_or_create(user=formlist_user)

    if profile.require_auth:
        authenticator = HttpDigestAuthenticator()
        if not authenticator.authenticate(request):
            return authenticator.build_challenge_response()

        # unauthorized if user in auth request does not match user in path
        # unauthorized if user not active
        if not request.user.is_active:
            return HttpResponseNotAuthorized()

    # filter private forms (where require_auth=False)
    # for users who are non-owner
    if request.user.username == profile.user.username:
        xforms = XForm.objects.filter(downloadable=True,
                                      user__username__iexact=username)
    else:
        xforms = XForm.objects.filter(downloadable=True,
                                      user__username__iexact=username,
                                      require_auth=False)

    audit = {}
    audit_log(Actions.USER_FORMLIST_REQUESTED, request.user, formlist_user,
              _("Requested forms list."), audit, request)

    data = {
        'host': request.build_absolute_uri().replace(
            request.get_full_path(), ''),
        'xforms': xforms
    }
    response = render(request, "xformsList.xml", data,
                      content_type="text/xml; charset=utf-8")
    response['X-OpenRosa-Version'] = '1.0'
    tz = pytz.timezone(settings.TIME_ZONE)
    dt = datetime.now(tz).strftime('%a, %d %b %Y %H:%M:%S %Z')
    response['Date'] = dt

    return response


@require_GET
def xformsManifest(request, username, id_string):  # noqa
    xform = get_object_or_404(
        XForm, id_string__exact=id_string, user__username__iexact=username
    )
    form_list_user = xform.user
    profile, created = UserProfile.objects.get_or_create(user=form_list_user)

    if profile.require_auth:
        authenticator = HttpDigestAuthenticator()
        if not authenticator.authenticate(request):
            return authenticator.build_challenge_response()

    response = render(request, "xformsManifest.xml", {
        'host': request.build_absolute_uri().replace(
            request.get_full_path(), ''),
        'media_files': MetaData.media_upload(xform, download=True)
    }, content_type="text/xml; charset=utf-8")
    response['X-OpenRosa-Version'] = '1.0'
    tz = pytz.timezone(settings.TIME_ZONE)
    dt = datetime.now(tz).strftime('%a, %d %b %Y %H:%M:%S %Z')
    response['Date'] = dt

    return response


@require_http_methods(["HEAD", "POST"])
@csrf_exempt
def submission(request, username=None):
    if username:
        formlist_user = get_object_or_404(User, username__iexact=username)
        profile, created = UserProfile.objects.get_or_create(
            user=formlist_user)

        if profile.require_auth:
            authenticator = HttpDigestAuthenticator()
            if not authenticator.authenticate(request):
                return authenticator.build_challenge_response()

    if request.method == 'HEAD':
        response = OpenRosaResponse(status=204)
        if username:
            response['Location'] = request.build_absolute_uri().replace(
                request.get_full_path(), '/%s/submission' % username)
        else:
            response['Location'] = request.build_absolute_uri().replace(
                request.get_full_path(), '/submission')
        return response

    xml_file_list = []
    media_files = []

    # request.FILES is a django.utils.datastructures.MultiValueDict
    # for each key we have a list of values
    try:
        xml_file_list = list(request.FILES.pop("xml_submission_file", []))
        if len(xml_file_list) != 1:
            return OpenRosaResponseBadRequest(
                _("There should be a single XML submission file.")
            )
        # save this XML file and media files as attachments
        media_files = list(request.FILES.values())

        # get uuid from post request
        uuid = request.POST.get('uuid')

        error, instance = safe_create_instance(
            username, xml_file_list[0], media_files, uuid, request)

        if error:
            return error
        elif instance is None:
            return OpenRosaResponseBadRequest(
                _("Unable to create submission."))

        audit = {
            "xform": instance.xform.id_string
        }
        audit_log(
            Actions.SUBMISSION_CREATED, request.user, instance.xform.user,
            _("Created submission on form %(id_string)s.") %
            {
                "id_string": instance.xform.id_string
            }, audit, request)

        response = _submission_response(request, instance)

        # ODK needs two things for a form to be considered successful
        # 1) the status code needs to be 201 (created)
        # 2) The location header needs to be set to the host it posted to
        response.status_code = 201
        response['Location'] = request.build_absolute_uri(request.path)
        return response
    except IOError as e:
        if _bad_request(e):
            return OpenRosaResponseBadRequest(
                _("File transfer interruption."))
        else:
            raise
    finally:
        for xml_file in xml_file_list:
            xml_file.close()
        for media_file in media_files:
            media_file.close()


def download_xform(request, username, id_string):
    user = get_object_or_404(User, username__iexact=username)
    xform = get_object_or_404(XForm,
                              user=user, id_string__exact=id_string)
    profile, created =\
        UserProfile.objects.get_or_create(user=user)

    if profile.require_auth:
        authenticator = HttpDigestAuthenticator()
        if not authenticator.authenticate(request):
            return authenticator.build_challenge_response()
    audit = {
        "xform": xform.id_string
    }
    audit_log(
        Actions.FORM_XML_DOWNLOADED, request.user, xform.user,
        _("Downloaded XML for form '%(id_string)s'.") %
        {
            "id_string": xform.id_string
        }, audit, request)
    response = response_with_mimetype_and_name('xml', id_string,
                                               show_date=False)
    response.content = xform.xml
    return response


def download_xlsform(request, username, id_string):
    xform = get_object_or_404(XForm,
                              user__username__iexact=username,
                              id_string__exact=id_string)
    owner = User.objects.get(username__iexact=username)
    helper_auth_helper(request)

    if not has_permission(xform, owner, request, xform.shared):
        return HttpResponseForbidden('Not shared.')

    file_path = xform.xls.name
    default_storage = get_storage_class()()

    if file_path != '' and default_storage.exists(file_path):
        audit = {
            "xform": xform.id_string
        }
        audit_log(
            Actions.FORM_XLS_DOWNLOADED, request.user, xform.user,
            _("Downloaded XLS file for form '%(id_string)s'.") %
            {
                "id_string": xform.id_string
            }, audit, request)

        if file_path.endswith('.csv'):
            with default_storage.open(file_path) as ff:
                xls_io = convert_csv_to_xls(ff.read())
                response = StreamingHttpResponse(
                    xls_io, content_type='application/vnd.ms-excel; charset=utf-8')
                response[
                    'Content-Disposition'] = 'attachment; filename=%s.xls' % xform.id_string
                return response

        split_path = file_path.split(os.extsep)
        extension = 'xls'

        if len(split_path) > 1:
            extension = split_path[len(split_path) - 1]

        response = response_with_mimetype_and_name(
            'vnd.ms-excel', id_string, show_date=False, extension=extension,
            file_path=file_path)

        return response

    else:
        messages.add_message(request, messages.WARNING,
                             _('No XLS file for your form '
                               '<strong>%(id)s</strong>')
                             % {'id': id_string})

        return HttpResponseRedirect("/%s" % username)


def download_jsonform(request, username, id_string):
    owner = get_object_or_404(User, username__iexact=username)
    xform = get_object_or_404(XForm, user__username__iexact=username,
                              id_string__exact=id_string)
    if request.method == "OPTIONS":
        response = HttpResponse()
        add_cors_headers(response)
        return response
    helper_auth_helper(request)
    if not has_permission(xform, owner, request, xform.shared):
        response = HttpResponseForbidden(_('Not shared.'))
        add_cors_headers(response)
        return response
    response = response_with_mimetype_and_name('json', id_string,
                                               show_date=False)
    if 'callback' in request.GET and request.GET.get('callback') != '':
        callback = request.GET.get('callback')
        response.content = "%s(%s)" % (callback, xform.json)
    else:
        add_cors_headers(response)
        response.content = xform.json
    return response


def view_submission_list(request, username):
    form_user = get_object_or_404(User, username__iexact=username)
    profile, created = \
        UserProfile.objects.get_or_create(user=form_user)
    authenticator = HttpDigestAuthenticator()
    if not authenticator.authenticate(request):
        return authenticator.build_challenge_response()
    id_string = request.GET.get('formId', None)
    xform = get_object_or_404(
        XForm, id_string__exact=id_string, user__username__iexact=username)
    if not has_permission(xform, form_user, request, xform.shared_data):
        return HttpResponseForbidden('Not shared.')
    num_entries = request.GET.get('numEntries', None)
    cursor = request.GET.get('cursor', None)
    instances = xform.instances.order_by('pk')

    cursor = _parse_int(cursor)
    if cursor:
        instances = instances.filter(pk__gt=cursor)

    num_entries = _parse_int(num_entries)
    if num_entries:
        instances = instances[:num_entries]

    data = {'instances': instances}

    resumptionCursor = 0
    if instances.count():
        last_instance = instances[instances.count() - 1]
        resumptionCursor = last_instance.pk
    elif instances.count() == 0 and cursor:
        resumptionCursor = cursor

    data['resumptionCursor'] = resumptionCursor

    return render(
        request, 'submissionList.xml', data,
        content_type="text/xml; charset=utf-8")


def view_download_submission(request, username):
    form_user = get_object_or_404(User, username__iexact=username)
    profile, created = \
        UserProfile.objects.get_or_create(user=form_user)
    authenticator = HttpDigestAuthenticator()
    if not authenticator.authenticate(request):
        return authenticator.build_challenge_response()
    data = {}
    formId = request.GET.get('formId', None)
    if not isinstance(formId, string_types):
        return HttpResponseBadRequest()

    id_string = formId[0:formId.find('[')]
    form_id_parts = formId.split('/')
    if form_id_parts.__len__() < 2:
        return HttpResponseBadRequest()

    uuid = _extract_uuid(form_id_parts[1])
    instance = get_object_or_404(
        Instance, xform__id_string__exact=id_string, uuid=uuid,
        xform__user__username=username)
    xform = instance.xform
    if not has_permission(xform, form_user, request, xform.shared_data):
        return HttpResponseForbidden('Not shared.')
    submission_xml_root_node = instance.get_root_node()
    submission_xml_root_node.setAttribute(
        'instanceID', 'uuid:%s' % instance.uuid)
    submission_xml_root_node.setAttribute(
        'submissionDate', instance.date_created.isoformat()
    )
    data['submission_data'] = submission_xml_root_node.toxml()
    data['media_files'] = Attachment.objects.filter(instance=instance)
    data['host'] = request.build_absolute_uri().replace(
        request.get_full_path(), '')

    return render(
        request, 'downloadSubmission.xml', data,
        content_type="text/xml; charset=utf-8")


@require_http_methods(["HEAD", "POST"])
@csrf_exempt
def form_upload(request, username):
    class DoXmlFormUpload():

        def __init__(self, xml_file, user):
            self.xml_file = xml_file
            self.user = user

        def publish(self):
            return publish_xml_form(self.xml_file, self.user)

    form_user = get_object_or_404(User, username__iexact=username)
    profile, created = \
        UserProfile.objects.get_or_create(user=form_user)
    authenticator = HttpDigestAuthenticator()
    if not authenticator.authenticate(request):
        return authenticator.build_challenge_response()
    if form_user != request.user:
        return HttpResponseForbidden(
            _("Not allowed to upload form[s] to %(user)s account." %
              {'user': form_user}))
    if request.method == 'HEAD':
        response = OpenRosaResponse(status=204)
        response['Location'] = request.build_absolute_uri().replace(
            request.get_full_path(), '/%s/formUpload' % form_user.username)
        return response
    xform_def = request.FILES.get('form_def_file', None)
    content = ""
    if isinstance(xform_def, File):
        do_form_upload = DoXmlFormUpload(xform_def, form_user)
        dd = publish_form(do_form_upload.publish)
        status = 201
        if isinstance(dd, XForm):
            content = _("%s successfully published." % dd.id_string)
        else:
            content = dd['text']
            if isinstance(content, Exception):
                content = content.message
                status = 500
            else:
                status = 400
    return OpenRosaResponse(content, status=status)


@user_passes_test(lambda u: u.is_superuser)
def superuser_stats(request, username):
    base_filename = '{}_{}_{}.zip'.format(
        re.sub('[^a-zA-Z0-9]', '-', request.get_host()),
        datetime_module.date.today(),
        datetime_module.datetime.now().microsecond
    )
    filename = os.path.join(
        request.user.username,
        'superuser_stats',
        base_filename
    )
    generate_stats_zip.delay(filename)
    template_ish = (
        '<html><head><title>Hello, superuser.</title></head>'
        '<body>Your report is being generated. Once finished, it will be '
        'available at <a href="{0}">{0}</a>. If you receive a 404, please '
        'refresh your browser periodically until your request succeeds.'
        '</body></html>'
    ).format(base_filename)
    return HttpResponse(template_ish)


@user_passes_test(lambda u: u.is_superuser)
def retrieve_superuser_stats(request, username, base_filename):
    filename = os.path.join(
        request.user.username,
        'superuser_stats',
        base_filename
    )
    default_storage = get_storage_class()()
    if not default_storage.exists(filename):
        raise Http404
    with default_storage.open(filename) as f:
        response = StreamingHttpResponse(f, content_type='application/zip')
        response['Content-Disposition'] = 'attachment;filename="{}"'.format(
            base_filename)
        return response
