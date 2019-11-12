#!/usr/bin/env python
# encoding=utf-8
from __future__ import print_function
import logging
import os
import sys
from django.utils import timezone

south_logger = logging.getLogger('south')
south_logger.setLevel(logging.INFO)

if __name__ == "__main__":
    # altered for new settings layout
    if not any([arg.startswith('--settings=') for arg in sys.argv]):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE",
                              "onadata.settings.kc_environ")
        print('Your environment is:"{}"'.format(
            os.environ['DJANGO_SETTINGS_MODULE']))
        print(timezone.now())
    print(timezone.get_current_timezone_name())

    from django.core.management import execute_from_command_line

    #print timezone.now()
    #execute_from_command_line(sys.argv)
