#
# (C) Copyright 2017- ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
#
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.
#

"""
ipython is not None when running a notebook
"""

import logging
import sys


ipython_active = None


def is_ipython_active():
    global ipython_active
    if ipython_active is None:
        try:
            from IPython import get_ipython

            ipython_active = get_ipython() is not None
        except Exception:
            ipython_active = False
    return ipython_active