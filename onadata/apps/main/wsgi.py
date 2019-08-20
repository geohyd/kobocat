from django.core.wsgi import get_wsgi_application

import os
os.environ["DJANGO_SETTINGS_MODULE"] = "onadata.settings.kc_environ"

application = get_wsgi_application()
