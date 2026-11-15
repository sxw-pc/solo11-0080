      * SYNTHETIC BALANCE COPYBOOK - SIGNED COMP-3 AND DISPLAY DECIMAL
       01  BALANCE-RECORD.
           05  REC-TYPE            PIC X(1).
           05  ACCOUNT-ID          PIC X(12).
           05  AMOUNT              PIC S9(9)V9(2) USAGE COMP-3.
           05  RATE                PIC 9(3)V9(4).
           05  BOOK-DATE           PIC 9(8).
           05  MEMO                PIC X(30).
