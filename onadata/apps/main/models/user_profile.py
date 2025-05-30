# coding: utf-8
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.utils import timezone
from django.utils.translation import gettext_lazy
from guardian.conf import settings as guardian_settings
from guardian.shortcuts import get_perms_for_model, assign_perm
from rest_framework.authtoken.models import Token

from onadata.apps.logger.fields import LazyDefaultBooleanField
from onadata.apps.main.signals import set_api_permissions
from onadata.libs.utils.country_field import COUNTRIES
from onadata.libs.utils.gravatar import get_gravatar_img_link, gravatar_exists


class UserProfile(models.Model):
    # This field is required.
    user = models.OneToOneField(User, related_name='profile', on_delete=models.CASCADE)

    # Other fields here
    name = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=255, blank=True)
    country = models.CharField(max_length=2, choices=COUNTRIES, blank=True)
    organization = models.CharField(max_length=255, blank=True)
    home_page = models.CharField(max_length=255, blank=True)
    twitter = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    require_auth = models.BooleanField(
        default=False,
        verbose_name=gettext_lazy(
            "Require authentication to see forms and submit data"
        )
    )
    address = models.CharField(max_length=255, blank=True)
    phonenumber = models.CharField(max_length=30, blank=True)
    num_of_submissions = models.IntegerField(default=0)
    attachment_storage_bytes = models.BigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)
    is_mfa_active = LazyDefaultBooleanField(default=False)
    validated_password = models.BooleanField(default=True)

    def __str__(self):
        return '%s[%s]' % (self.name, self.user.username)

    @property
    def gravatar(self):
        return get_gravatar_img_link(self.user)

    @property
    def gravatar_exists(self):
        return gravatar_exists(self.user)

    @property
    def twitter_clean(self):
        if self.twitter.startswith("@"):
            return self.twitter[1:]
        return self.twitter

    class Meta:
        app_label = 'main'
        permissions = (
            ('can_add_xform', "Can add/upload an xform to user profile"),
            ('view_profile', "Can view user profile"),
        )


def create_auth_token(sender, instance=None, created=False, **kwargs):
    if created:
        Token.objects.create(user=instance)


post_save.connect(create_auth_token, sender=User, dispatch_uid='auth_token')

post_save.connect(set_api_permissions, sender=User,
                  dispatch_uid='set_api_permissions')


def set_object_permissions(sender, instance=None, created=False, **kwargs):
    if created:
        for perm in get_perms_for_model(UserProfile):
            assign_perm(perm.codename, instance.user, instance)


post_save.connect(set_object_permissions, sender=UserProfile,
                  dispatch_uid='set_object_permissions')


def default_user_profile_require_auth(
        sender, instance, created, raw, **kwargs):
    if raw or not created:
        return
    instance.require_auth = \
        settings.REQUIRE_AUTHENTICATION_TO_SEE_FORMS_AND_SUBMIT_DATA_DEFAULT
    instance.save()


post_save.connect(default_user_profile_require_auth,
                  sender=UserProfile,
                  dispatch_uid='default_user_profile_require_auth')


def get_anonymous_user_instance(User):
    """
    Force `AnonymousUser` to be saved with `pk` == `ANONYMOUS_USER_ID`
    :param User: User class
    :return: User instance
    """

    return User(pk=settings.ANONYMOUS_USER_ID,
                username=guardian_settings.ANONYMOUS_USER_NAME)
