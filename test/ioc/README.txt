### Development IOC for loadedQ ###

############
# Overview #
############

This soft IOC is intended for development testing of the loadedQ daemon. It has
one component, a "traditional" soft IOC setup that includes some empty records,
and simple python script for providing IOC initialization and logic.

############
# Comannds #
############

#To Start
scripts/daemon.sh

# To Stop
scripts/stop.sh

Additional IOC commands are available to get access to IOC's console, or to
start it outside of procServ.

#########
# Notes #
#########

This IOC presents OPS PVs, but with the 'adamc:' prefix.  So R123GSET becomes
adamc:R123GSET.  The current behavior of the daq software is to add the "adamc:"
prefix only if being run in TestMode.
