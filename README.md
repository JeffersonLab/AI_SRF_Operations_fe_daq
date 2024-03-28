# AI_SRF_Operations_fe_daq
Software for interacting SRF Cavities and NDX detectors for Field Emission Data Acquisition

## Overview
This software is used to control the RF settings and NDX detectors associated with a linac so that we can collect data useful in modeling field emission.  This software controls NDX and RF cavity settings, while monitoring signals from the Cryo, RF, and NDX systems to determine if any conditions have arrisen that would necessitate the application should pause changes.

Multiple sample strategies have been written, but only the `random_sample_gradient_scan` has been recently exercised extensively.  The `simple_gradient_scan` strategy has also been exercised less, however it only modifies a single cavity at a time so it presents fewer risks.

This application stores both a verbose application log and a more concise data lookup index.  The data itself is expected to be captured by MYA, and the index file is used by the fe_mya_data tool to produce a CSV of the data.  See the links below for more details.
- https://github.com/JeffersonLab/AI_SRF_Operations_FE
- https://github.com/JeffersonLab/AI_SRF_Operations_FE/tree/main/AI_SRF_Operations_FE/data_prep/fe_mya_getter

Contact Adam Carpenter with any further questions about this software.

## CSUE
This software is organized to independent of CSUE, however the intent is that it is installed to CSUE.  As a consequence some of the internal paths may need to be changed in order to work outside of CSUE.

To install in CSUE:
- Create an application through CSUE tools
- Clone the repo to `<app_name>/dvl/src/fe_daq`
- Add most of the fe_daq directory to CVS, excluding things like pycaches, etc.
- Define any dependencies through the supports directory (e.g. python3.7)
- the fe_daq.sh script is intended to be installed in the CSUE bin directory with the template header for CSUE to manage

*Note*: This has already been installed in CSUE under the app name fe_daq.

## Testing
This application is designed to be tested against a softIoc running mocked up copies of real CEBAF PVs, but with a prefix added to the names.  In addition, if you run these tests, do not run them only in CEBAF's development enclaves as a second layer of protection for accidental impact on CEBAF.

To do full integration test in the dev fiefdom:
- clone the repo to a local directory on an accelerator system
- In one window run `test/ioc/script/daemon.sh`
- In another window
  - Run `bin/fe_daq.sh -t -l <LINAC> <sample_strategy> <options>`  (sample strategy usually would be `random_sample_gradient_scan`)
  - To stop the softIoc and it's controller script run `test/ioc/script/stop.sh`
- In a third window
  - Optionally run livePlot to monitor any PVs of interest.  Use the prefix "adamc:" for all PVs (e.g., adamc:R123GMES)
  - Optionally run `test/ioc/scripts/trigger_event.py` to simulate differet types of events (e.g., RF fault, JT valve too open, etc.) that should cause the `fe_daq` application to pause

To do a mix of unit and integration tests in the dev fiefdom:
- clone the repo to a local directory on an accelerator system
- In one window run `test/ioc/script/daemon.sh`
- In another window or an IDE
  - cd application root (`fe_daq/`)
  - run `python -m unittest`
  - Note: You may need to add `src/` to your PYTHONPATH for this to work.  I typically configure these tests in the PyCharm IDE which probably does some path magic behind the scenes.
