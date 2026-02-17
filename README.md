## Command to run long form simulation 
python sim.py --iters=20 --loglevel=info --num-pieces=32 --blocks-per-piece=32 --min-bw=32 --max-bw=64 --max-round=1000 Seed,2 CpcwStd,10


## Client setups
 - Standard: 
    - when able to download, it decides to send requests for only the rarest pieces that each client has. Look at rare piece calculation and sort. When examining what to request, we intersect rarity list with available pieces from peer, and 
    - 