# ShotLog replay simulator

`shot_log_simulator.py` replays a completed experiment by gradually copying RAW
images and motor CSV files into a dedicated test directory. It mimics the
cloud synchronisation behaviour so that `shot_log.py` can be exercised against
realistic filesystem changes without touching the original data.

## Running the simulator

```
python shot_log_simulator.py
```

Select the following inputs in the GUI:

1. **RAW source root**: directory that contains the `*_RAW_DATA` folder
   produced by the experiment.
2. **Motor initial CSV**: the read-only CSV with starting motor positions.
3. **Motor history CSV**: the read-only CSV containing timestamped motor moves.
4. **Test root destination**: empty directory where the simulator writes the
   replayed RAW tree and CSV copies.

## Parameters

* **Cloud period (s)**: interval between sync ticks (default 30s).
* **Cloud jitter max (s)**: maximum random delay applied to each RAW file and
  motor event before it becomes visible. The jitter is re-drawn when the start
  time is set.
* **Start time**: optional timestamp (`YYYY-MM-DD HH:MM:SS` or ISO formats).
  If left empty, the simulator starts from the earliest RAW/motor timestamp.

## Workflow

1. Press **Set start time** to load the sources, generate jittered visibility
   times, reset the test root, and populate it with everything visible up to the
   chosen start time. The test root is rebuilt from scratch each time you press
   this button after stopping the simulation.
2. Press **Start** to begin real-time replay. Files and motor rows are added at
   every cloud period, with jitter applied to their visibility times. The
   simulator rewrites the motor history CSV at each tick to reflect the current
   subset of events.
3. Press **Update now** to trigger an immediate sync tick without waiting for
   the period. The simulation must have been seeded first (via Set start time or
   Start).
4. Press **Stop** to halt the periodic ticks. The current test root state is
   preserved so you can run `shot_log.py` against it.

## Notes

* The simulator never modifies source files; it only reads them.
* `motor_history.csv` in the test root is rebuilt at each sync with a header
  `time,motor,old_pos,new_pos` and includes only events visible at that time.
* File modification times are preserved using `copy2` plus an explicit `utime`
  to reflect the original acquisition timestamps.
* If you change the start time after stopping the simulation, the test root is
  cleared and reconstructed to match the new reference point for correctness.
