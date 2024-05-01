import sys
import os

# Do this so that all of the relative imports within fe_daq are found.  There is probably a better way, but this has
# been a struggle.
app_root = os.path.realpath(os.path.join(os.path.basename(__file__), ".."))
sys.path.append(f"{app_root}/src")

from fe_daq import app_config as config
config.app_root = app_root
