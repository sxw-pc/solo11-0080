      * SYNTHETIC CUSTOMER MASTER COPYBOOK - DEMO DATA ONLY
       01  CUSTOMER-RECORD.
           05  REC-TYPE            PIC X(1).
           05  CUSTOMER-ID         PIC X(10).
           05  CUSTOMER-NAME       PIC X(20).
           05  OPEN-DATE           PIC 9(8).
           05  STATUS              PIC X(2).
           05  BALANCE             PIC S9(7)V9(2) USAGE COMP-3.
           05  TAG-COUNT           PIC 9(2).
           05  TAGS OCCURS 3
      -        TIMES.
               10  TAG-VALUE       PIC X(8).
           05  RENEW-DATE          PIC 9(8).
           05  CONTROL-NO          PIC 9(6).
