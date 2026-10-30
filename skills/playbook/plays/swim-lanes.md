# swim-lanes

Same task, N lanes, run in parallel and blind to each other. You compare at the
finish and merge.

## Run it

1. Write one task. Give each lane its own brief for it: a different model, a
   different effort, or a different lens (correctness against source, the
   user who must follow it, security). An adversarial brief here is how to
   red-team.
2. Run one round with every lane (see "Running a round").
3. Merge:
   - findings more than one lane reached: still verify them; shared material
     produces shared mistakes;
   - findings only one lane reached: verify before adopting; a lone finding is
     often the valuable one;
   - lanes that contradict each other: settle it against source, or take it
     to a roundtable.
4. Report the merge.

## Proposals

When the lanes each propose something (a design, a fix, a name), add a judging
round: send each proposal in full, unattributed, to a lane that did not write
it (with two lanes, they swap; prefer a judge on a different model, since a
model may recognise its own prose). Pick one or merge the best parts; the
judges advise.
