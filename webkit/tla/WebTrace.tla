------------------------------ MODULE WebTrace ------------------------------
(***************************************************************************)
(* Generic trace ordering for web-app trace validation.                    *)
(*                                                                         *)
(* Reads a trace built by webkit/tracebuilder/build.py. Each event took    *)
(* effect somewhere in its interval [lo, hi]. An event may be matched once *)
(* every event that must precede it has been:                             *)
(*   - earlier events of the same actor (program order), and              *)
(*   - events of other actors whose interval ends before this one starts. *)
(* Overlapping events of different actors may be matched in either order,  *)
(* so TLC searches the interleavings the recording leaves open (as in      *)
(* Specula's Category B traces).                                          *)
(*                                                                         *)
(* An app trace spec EXTENDS this module and its app model, matches        *)
(* events with Consume(j) for j \in Ready, and checks TraceNotDone as an   *)
(* invariant: a violation means some behavior matched Cut events (Cut =    *)
(* TraceLen to require the whole trace).                                   *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets, Json

CONSTANTS TracePath, Cut

TraceLog == ndJsonDeserialize(TracePath)
Header == TraceLog[1]
TraceLen == Len(TraceLog) - 1
Ev(i) == TraceLog[i + 1]

\* Events that must be matched before event j (computed once by TLC: constant-level).
MustPrecede ==
    [j \in 1..TraceLen |->
        {i \in 1..TraceLen :
            /\ i # j
            /\ \/ Ev(i).actor = Ev(j).actor /\ i < j
               \/ Ev(i).hi < Ev(j).lo}]

VARIABLE done   \* events matched so far

Ready == {j \in 1..TraceLen : j \notin done /\ MustPrecede[j] \subseteq done}

TraceInit == done = {}
Consume(j) == done' = done \cup {j}

\* Violated when Cut events have been matched.
TraceNotDone == Cardinality(done) < Cut

=============================================================================
