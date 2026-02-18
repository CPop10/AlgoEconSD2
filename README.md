## Command to run long form simulation 
python sim.py --iters=20 --loglevel=info --num-pieces=32 --blocks-per-piece=32 --min-bw=32 --max-bw=64 --max-round=1000 Seed,2 CpcwStd,10

## Client setups
 - Standard: 
    - when able to download, it decides to send requests for only the rarest pieces that each client has. Look at rare piece calculation and sort. When examining what to request, we intersect rarity list with available pieces from peer, and 
    - 

Tit-for-tat is effectively broken
In cpcwstd.py (line 187), this check is wrong:
if round_num in history.downloads
history.downloads is a list of round-lists, so this is almost always false. Result: _get_download_rates() returns {} even when peers uploaded to you, so regular unchoking never works as intended.

Request pattern causes heavy duplicate waste
In cpcwstd.py (line 47) and cpcwstd.py (line 58), you pick the same global rarest list for every peer.
That means you ask many peers for the same few pieces each round.
In the simulator, duplicate requests for the same piece don’t stack (sim.py (line 234), sim.py (line 248)), so much of that request effort is wasted.
Quick check I ran: same setup, CpcwStd made 36 requests covering only 3 unique pieces; Dummy made 36 requests covering 20 unique pieces.

Optimistic unchoke can go stale
In cpcwstd.py (line 120) + cpcwstd.py (line 233), optimistic peer is only refreshed every 3 rounds and reused even if no longer interested this round, so you can spend upload on a peer that didn’t request from you.
----
Patched cpcwstd.py to fix the underperformance.

Added request de-duplication + peer shuffling so one round doesn’t request the same few pieces from everyone (cpcwstd.py (line 45), cpcwstd.py (line 52)).
Fixed download-rate accounting to read actual round slices from history (cpcwstd.py (line 176), cpcwstd.py (line 186)).
Updated regular unchoke selection to include all interested peers (defaulting missing rates to 0.0) so slots don’t collapse (cpcwstd.py (line 203)).
Refreshed optimistic unchoke when stale or duplicated with regular peers (cpcwstd.py (line 108), cpcwstd.py (line 122)).
Validation:

python -m py_compile cpcwstd.py passed.
Same 40-iteration benchmark config:
Dummy: completion averages about 28.68 to 29.18 rounds.
patched CpcwStd: completion averages about 25.63 to 25.98 rounds.
If you want, I can tune an endgame mode next (controlled duplicate requests only near completion) for another small gain.