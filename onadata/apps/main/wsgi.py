# coding: utf-8
from __future__ import unicode_literals, print_function, division, absolute_import

from django.core.wsgi import get_wsgi_application

import os
os.environ["DJANGO_SETTINGS_MODULE"] = "onadata.settings.production"

application = get_wsgi_application()
