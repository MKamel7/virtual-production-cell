(*
   Cell control, IEC 61131-3 Structured Text.

   This is the thing under test. Everything else in the repository exists to
   drive it: the plant simulation is a model it controls, the Modbus server is
   how the two are wired, and the Python PackML module is the executable
   specification this program implements.

   READ THAT LAST POINT BEFORE CHANGING ANYTHING HERE. The state machine below
   is not the authority. src/vpc/packml.py is, and it is verified exhaustively:
   every state reachable, Execute reachable from everywhere, Abort reachable
   from everywhere, and all 153 state and command combinations either
   transitioning or refused. This program was written to match that model, and
   if the two disagree, this one is wrong.

   The reason for that ordering is blunt: Structured Text cannot be executed on
   the machine this was developed on, and a seventeen state machine with a
   partial transition function is exactly the kind of thing that looks correct
   and is not.

   TARGET: written to plain IEC 61131-3 so it ports between runtimes. No vendor
   extensions, no vendor function blocks, no direct addressing outside the
   generated declarations. Tested on OpenPLC; intended for CODESYS.
*)

PROGRAM CellControl
VAR
    (* PackML state, as the numeric encoding PackTags uses. Names in comments
       because a reviewer reads the standard's names, not the numbers. *)
    PMLState        : INT := 9;     (* 9 = Aborted, the power on state *)

    (* Commands from the supervisory layer. ONE SHOT: every one of these is
       cleared at the end of the scan whether it was acted on or not, so a
       command that arrives in a state which ignores it is DISCARDED rather
       than latched. See the block at the end of the program for why. *)
    CmdReset        : BOOL;
    CmdStart        : BOOL;
    CmdStop         : BOOL;
    CmdHold         : BOOL;
    CmdUnhold       : BOOL;
    CmdSuspend      : BOOL;
    CmdUnsuspend    : BOOL;
    CmdAbort        : BOOL;
    CmdClear        : BOOL;

    (* Asking the safety channel for torque back. DELIBERATELY NOT CmdReset.
       CmdReset is consumed by the state machine, which clears it in the same
       scan it acts on it, and the output block below runs after that. Deriving
       the safety request from CmdReset therefore meant it could never assert
       from Stopped, only from states that happen to ignore CmdReset. A safety
       function whose behaviour depends on which branch of a CASE ran first is
       one nobody can reason about. *)
    CmdSafetyReset  : BOOL;

    (* Acting states finish when their work does. These stand in for the
       equipment timing the model deliberately does not carry. *)
    ActingTimer     : INT := 0;
    ACTING_SCANS    : INT := 2;

    (* Edge detection on the guard, so opening it aborts once rather than
       continuously re-triggering while it stays open. *)
    GuardWasClosed  : BOOL := TRUE;

    (* Link watchdog. SCAN_COUNT is the plant's heartbeat: it increments every
       plant scan, so a value that stops changing means the plant has stopped
       or the link is dead. Started at a value the plant will not publish on
       its first scan, so a link that never comes up is a fault rather than a
       match. *)
    LastScanCount   : WORD := 16#FFFF;
    LinkStallScans  : INT := 0;
    LinkFault       : BOOL := TRUE;
    LINK_STALL_LIMIT: INT := 10;
END_VAR

(* PackML state encoding, per the PackTags convention.
   1 Clearing   2 Stopped    3 Starting   4 Idle       5 Suspended
   6 Execute    7 Stopping   8 Aborting   9 Aborted   10 Holding
  11 Held      12 Unholding 13 Suspending 14 Unsuspending
  15 Resetting 16 Completing 17 Complete                              *)

(* ------------------------------------------------------------------
   LINK WATCHDOG, and it runs before the safety block because a stale input is
   worse than a bad one.

   The Modbus master is configured to zero its inputs on a channel error, so a
   dropped link already reads as SAFETY_OK false and stops the cell. That is
   necessary and not sufficient, because it only covers the failures the master
   NOTICES. A plant process that hangs while its TCP connection stays open
   answers nothing, and depending on the error handling setting the master may
   keep serving the last values it received. The controller would then run a
   machine whose real state it has not seen for minutes.

   SCAN_COUNT is the answer and it costs nothing: the plant increments it every
   scan, so it is already a heartbeat. A value that stops changing means the
   plant has stopped, whatever the socket believes. This is the difference
   between checking that a link is OPEN and checking that it is ALIVE.
   ------------------------------------------------------------------ *)
IF SCAN_COUNT = LastScanCount THEN
    LinkStallScans := LinkStallScans + 1;
ELSE
    LinkStallScans := 0;
END_IF;
LastScanCount := SCAN_COUNT;

LinkFault := LinkStallScans >= LINK_STALL_LIMIT;

IF LinkFault THEN
    (* Not a stop. A controller that cannot see the plant cannot know it is
       safe to decelerate politely, so this takes the abort path like any other
       loss of confidence in the machine's state. *)
    CmdAbort := TRUE;
END_IF;

(* ------------------------------------------------------------------
   SAFETY FIRST, and unconditionally.

   This block runs before the state machine and before any output logic,
   and nothing below it can override the result. Two reasons it is written
   this way rather than as a state in the machine:

   The guard is not a request. An operator opening a door is not asking the
   program for permission, and a program that could decline would be a program
   that could be wrong about it.

   SAFETY_OK is an INPUT and never an output. The PLC reads whether torque is
   available; it cannot grant it. Restoring torque is a separate deliberate act
   through the safety channel, which is why closing the door does not restart
   the line.
   ------------------------------------------------------------------ *)
IF GuardWasClosed AND NOT GUARD_CLOSED THEN
    CmdAbort := TRUE;                (* opened: abort once, on the edge *)
END_IF;
GuardWasClosed := GUARD_CLOSED;

IF NOT SAFETY_OK THEN
    (* Torque is withheld. Drop every actuator regardless of state, because
       leaving an output asserted while torque returns is how a machine starts
       mid-cycle. *)
    CONVEYOR_RUN   := FALSE;
    FILLER_DOSE    := FALSE;
    CAPPER_ACTUATE := FALSE;
    REJECT_EJECT   := FALSE;
END_IF;

(* ------------------------------------------------------------------
   PackML state machine.

   Commands are checked in the order the standard ranks them: Abort outranks
   Stop, and both outrank everything else. Getting that order wrong gives a
   machine that decelerates politely when somebody hits the emergency button.
   ------------------------------------------------------------------ *)

(* Abort: legal everywhere except where the machine is already going there *)
IF CmdAbort AND PMLState <> 8 AND PMLState <> 9 THEN
    PMLState := 8;                   (* Aborting *)
    ActingTimer := 0;
    CmdAbort := FALSE;

(* Stop: legal everywhere except where an abort outranks it or it is redundant *)
ELSIF CmdStop AND PMLState <> 7 AND PMLState <> 2 AND PMLState <> 8
      AND PMLState <> 9 AND PMLState <> 1 THEN
    PMLState := 7;                   (* Stopping *)
    ActingTimer := 0;
    CmdStop := FALSE;

ELSE
    CASE PMLState OF

    9:  (* Aborted: wait *)
        IF CmdClear THEN
            PMLState := 1; ActingTimer := 0; CmdClear := FALSE;
        END_IF;

    2:  (* Stopped: wait *)
        IF CmdReset THEN
            PMLState := 15; ActingTimer := 0; CmdReset := FALSE;
        END_IF;

    4:  (* Idle: wait *)
        IF CmdStart THEN
            PMLState := 3; ActingTimer := 0; CmdStart := FALSE;
        END_IF;

    17: (* Complete: wait *)
        IF CmdReset THEN
            PMLState := 15; ActingTimer := 0; CmdReset := FALSE;
        END_IF;

    11: (* Held: wait *)
        IF CmdUnhold THEN
            PMLState := 12; ActingTimer := 0; CmdUnhold := FALSE;
        END_IF;

    5:  (* Suspended: wait *)
        IF CmdUnsuspend THEN
            PMLState := 14; ActingTimer := 0; CmdUnsuspend := FALSE;
        END_IF;

    6:  (* Execute: producing. Hold and Suspend are the only commands, and
           the state completes when the batch does. *)
        IF CmdHold THEN
            PMLState := 10; ActingTimer := 0; CmdHold := FALSE;
        ELSIF CmdSuspend THEN
            PMLState := 13; ActingTimer := 0; CmdSuspend := FALSE;
        END_IF;

    (* Acting states: no command moves them, only their own completion.
       This is the state complete transition, and it belongs to the machine. *)
    1, 3, 7, 8, 10, 12, 13, 14, 15, 16:
        ActingTimer := ActingTimer + 1;
        IF ActingTimer >= ACTING_SCANS THEN
            ActingTimer := 0;
            CASE PMLState OF
             1: PMLState := 2;       (* Clearing    -> Stopped   *)
             3: PMLState := 6;       (* Starting    -> Execute   *)
             7: PMLState := 2;       (* Stopping    -> Stopped   *)
             8: PMLState := 9;       (* Aborting    -> Aborted   *)
            10: PMLState := 11;      (* Holding     -> Held      *)
            12: PMLState := 6;       (* Unholding   -> Execute   *)
            13: PMLState := 5;       (* Suspending  -> Suspended *)
            14: PMLState := 6;       (* Unsuspending-> Execute   *)
            15: PMLState := 4;       (* Resetting   -> Idle      *)
            16: PMLState := 17;      (* Completing  -> Complete  *)
            END_CASE;
        END_IF;

    END_CASE;
END_IF;

(* ------------------------------------------------------------------
   Outputs.

   Actuators run only in Execute. Every other state, including every acting
   state, leaves the cell still. That is deliberate and it is stricter than the
   standard requires: a machine that keeps doing things while it is Holding is
   a machine whose Held state means nothing.
   ------------------------------------------------------------------ *)
IF PMLState = 6 AND SAFETY_OK AND NOT LinkFault THEN

    (* The conveyor runs unless a station is still working on what is in front
       of it. This is what makes the line block rather than push product into
       a station that is not ready. *)
    CONVEYOR_RUN := NOT FILLER_BUSY AND NOT CAPPER_BUSY;

    (* Dose while a bottle is present and unfilled. FILLER_BUSY drops when the
       fill completes, which is the plant telling us, not a timer here. *)
    FILLER_DOSE := PRODUCT_AT_FILLER AND FILLER_BUSY;

    CAPPER_ACTUATE := PRODUCT_AT_CAPPER AND CAPPER_BUSY;

    (* Reject only on a present product that actually failed. QC_FAIL is only
       meaningful while PRODUCT_AT_QC is set, so both are required. *)
    REJECT_EJECT := PRODUCT_AT_QC AND QC_FAIL;

ELSE
    CONVEYOR_RUN   := FALSE;
    FILLER_DOSE    := FALSE;
    CAPPER_ACTUATE := FALSE;
    REJECT_EJECT   := FALSE;
END_IF;

(* The reset request is an ASK, not a grant. The safety channel decides, it
   checks the guard itself rather than trusting this, and it acts on the RISING
   edge so a held request cannot restart the machine when a door closes.

   Driven by CmdSafetyReset rather than CmdReset: see the declaration. The
   guard and SAFETY_OK terms stay because asking while the guard is open, or
   while torque is already available, is asking for something meaningless, and
   a request that is only ever raised when it makes sense is one a reviewer can
   read at a glance. *)
SAFETY_RESET_REQUEST := CmdSafetyReset AND GUARD_CLOSED AND NOT SAFETY_OK;

(* ------------------------------------------------------------------
   COMMANDS ARE ONE SHOT, and this block is the whole reason.

   Every branch above clears the command it consumed. Nothing cleared a command
   that arrived in a state which ignores it, so those stayed latched, and the
   next time the machine entered a state that DOES consume them they fired with
   nobody asking. Send Start while the cell is Aborted, clear and reset it an
   hour later, and it begins producing on its own.

   Found on the running cell: CmdReset and CmdStart both sat TRUE in Execute,
   having been written while the machine was somewhere that ignored them. The
   cell was one Stop away from resetting itself.

   Discarding is the right answer rather than queueing. A command is an
   instruction about the machine's state NOW, and an operator who pressed Start
   against a machine that was not ready expects to press it again once it is,
   not to have the old press honoured at a moment they are no longer watching.
   That is also what makes the wait states mean anything: the machine waits for
   a decision rather than for the arrival of a decision made earlier.
   ------------------------------------------------------------------ *)
CmdReset       := FALSE;
CmdStart       := FALSE;
CmdStop        := FALSE;
CmdHold        := FALSE;
CmdUnhold      := FALSE;
CmdSuspend     := FALSE;
CmdUnsuspend   := FALSE;
CmdAbort       := FALSE;
CmdClear       := FALSE;
CmdSafetyReset := FALSE;

END_PROGRAM
