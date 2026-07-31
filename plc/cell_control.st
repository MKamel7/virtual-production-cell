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

    (* Commands from the supervisory layer. Latched by the HMI or OPC UA, and
       cleared here once acted on, so a held command cannot fire twice. *)
    CmdReset        : BOOL;
    CmdStart        : BOOL;
    CmdStop         : BOOL;
    CmdHold         : BOOL;
    CmdUnhold       : BOOL;
    CmdSuspend      : BOOL;
    CmdUnsuspend    : BOOL;
    CmdAbort        : BOOL;
    CmdClear        : BOOL;

    (* Acting states finish when their work does. These stand in for the
       equipment timing the model deliberately does not carry. *)
    ActingTimer     : INT := 0;
    ACTING_SCANS    : INT := 2;

    (* Edge detection on the guard, so opening it aborts once rather than
       continuously re-triggering while it stays open. *)
    GuardWasClosed  : BOOL := TRUE;
END_VAR

(* PackML state encoding, per the PackTags convention.
   1 Clearing   2 Stopped    3 Starting   4 Idle       5 Suspended
   6 Execute    7 Stopping   8 Aborting   9 Aborted   10 Holding
  11 Held      12 Unholding 13 Suspending 14 Unsuspending
  15 Resetting 16 Completing 17 Complete                              *)

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
IF PMLState = 6 AND SAFETY_OK THEN

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

(* The reset request is an ASK, not a grant. The safety channel decides, and it
   will refuse while the guard is open. *)
SAFETY_RESET_REQUEST := CmdReset AND GUARD_CLOSED AND NOT SAFETY_OK;

END_PROGRAM
