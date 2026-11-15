      * SYNTHETIC ACCOUNT COPYBOOK - REDEFINES + OCCURS + COMP-3
       01  ACCOUNT-RECORD.
           05  REC-TYPE            PIC X(1).
           05  ACCOUNT-KEY.
               10  REGION          PIC X(2).
               10  ACCOUNT-NO      PIC X(8).
           05  CONTACT.
               10  CONTACT-NAME    PIC X(16).
               10  CONTACT-PHONE   PIC X(8).
           05  CONTACT-V2 REDEFINES CONTACT.
               10  EMAIL           PIC X(24).
           05  SCORE               PIC 9(3).
           05  AMOUNT              PIC S9(5)V9(2) USAGE COMP-3.
           05  SCORE-HISTORY OCCURS 2 TIMES PIC 9(4).
