# coding: utf-8
from .base import *

################################
# Django Framework settings    #
################################

# Force `DEBUG` and `TEMPLATE_DEBUG` to `False`
DEBUG = False
TEMPLATES[0]['OPTIONS']['debug'] = False

# DATABASES = {
#     'default': {
#         'ENGINE': 'django.contrib.gis.db.backends.postgis',
#         'NAME': os.environ.get('PG_DB', 'kobo_db'),
#         'USER': os.environ.get('PG_USER', 'kobo'),
#         'PASSWORD': os.environ.get('PG_PASS', 'kobo'),
#         'HOST': os.environ.get('PG_HOST', '127.0.0.1'),
#         'PORT': os.environ.get('PG_PORT', '5432'),
#     }
# }
DATABASES['default']['ENGINE']='django.contrib.gis.db.backends.postgis'

TIME_ZONE = 'Europe/Paris'
#USE_TZ = True
USE_TZ = False

#If you want to add middleware to Kobocat
#MIDDLEWARE_CLASSES = ('antea.middleware.Middle', ) + MIDDLEWARE_CLASSES

#If you want change de max upload size on form.
#Need to match with nginx client_max_body_size config
#You cannot exceed ABSOLUTE_MAX_SIZE in enketo/public/js/src/module/connection.js, or change value to.
DEFAULT_CONTENT_LENGTH = 50000000

ADMINS = (
    (os.environ.get('DEFAULT_ADMIN_NAME', ''), os.environ.get('DEFAULT_ADMIN_MAIL', '')),
) + ADMINS
MANAGERS = ADMINS
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', '')
CORS_ORIGIN_WHITELIST = (
    #'dev.ona.io',
)

DEBUG = env.bool("DJANGO_DEBUG", False)
TEMPLATES[0]['OPTIONS']['debug'] = os.environ.get('TEMPLATE_DEBUG', 'False') == 'True'