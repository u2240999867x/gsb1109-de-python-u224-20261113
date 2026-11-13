~VERSION INFORMATION
 VERS.                  2.0:   CWLS LOG ASCII STANDARD -VERSION 2.0
 WRAP.                    NO:   ONE LINE PER DEPTH STEP
~WELL INFORMATION
#MNEM.UNIT       DATA TYPE    INFORMATION
#---------    -------------   ------------------------------
 STRT.M              1000.0:   START DEPTH
 STOP.M              1250.0:   STOP DEPTH
 STEP.M                50.0:   STEP
 NULL.              -999.25:   NULL VALUE
 WELL.      HANDOFF TEST WELL:
~PARAMETER INFORMATION
# OPENING (SPUD) HANDOVER - BATCH 1
#MNEM.UNIT        VALUE       DESCRIPTION
#---------    -------------   ------------------------------
 BHT .DEGC         35.5000:   BOTTOM HOLE TEMPERATURE BATCH 1
 PRE .PSI         100.0000:   PRESSURE SETTING BATCH 1 SEGMENT A 1000.0 - 1120.0 M
 PRE .PSI         110.0000:   PRESSURE SETTING BATCH 1 SEGMENT B 1120.0 - 1250.0 M
 FD  .K/M3        1000.0000:   FLUID DENSITY BATCH 1
 RMR .                  ABC:   REMARK OPENING VERSION
~PARAMETER INFORMATION
# COMPLETION HANDOVER - BATCH 2
#MNEM.UNIT        VALUE       DESCRIPTION
#---------    -------------   ------------------------------
 BHT .DEGC         40.0000:   BOTTOM HOLE TEMPERATURE BATCH 2
 PRE .PSI         120.0000:   PRESSURE SETTING BATCH 2
 FD  .G/CM3          1.0000:   FLUID DENSITY BATCH 2 RESTATED IN G/CM3
 RMR .                  XYZ:   REMARK COMPLETION VERSION
~CURVE INFORMATION
#MNEM.UNIT      API CODE      CURVE DESCRIPTION
#---------    -------------   ------------------------------
 DEPT.M                      :  1  DEPTH
 GAMN.GAPI                   :  2  GAMMA RAY
~Other
     Handover fixture: two ~P sections simulate an opening and a completion
     delivery of the same well, repeating mnemonics on purpose.
~A
      1000.0       80.0
      1050.0       82.0
      1100.0       81.5
      1150.0       83.0
      1200.0       84.0
      1250.0       79.0
