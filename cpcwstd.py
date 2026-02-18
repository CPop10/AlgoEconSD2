#!/usr/bin/python

# BitTorrent client implementing the reference protocol:
# - Rarest-first piece selection
# - Rate-based choking algorithm (leecher state)
# - Optimistic unchoking every 3 rounds
# - Equal bandwidth split across m=4 upload slots

import random
import logging
from collections import defaultdict

from messages import Upload, Request
from util import even_split
from peer import Peer

class CpcwStd(Peer):
    def post_init(self):
        print(("post_init(): %s here!" % self.id))
        
        # State for choking algorithm
        
        self.optimistic_unchoke_peer = None  # Current optimistic peer
        self.unchoked_peers = []  # Currently unchoked peers
        self.num_upload_slots = 4  # m = 4 as per protocol
        

    def requests(self, peers, history):
        """
        Implements rarest-first piece selection algorithm.
        Prioritizes requesting pieces that are least common across peers.
        """
        needed = lambda i: self.pieces[i] < self.conf.blocks_per_piece
        needed_pieces = list(filter(needed, list(range(len(self.pieces)))))
        
        if len(needed_pieces) == 0:
            return []  # We have everything!

        logging.debug("%s here: still need pieces %s" % (self.id, needed_pieces))

        
        # Sort pieces by rarity (rarest first)
        sorted_needed = self._sort_pieces_by_rarity(needed_pieces, peers)
        
        requests = []
        requested_pieces = set()

        # Symmetry breaking helps avoid all peers making identical choices.
        peers_shuffled = peers[:]
        random.shuffle(peers_shuffled)
        
        # Request rarest pieces first, but avoid duplicate piece requests in a round.
        for peer in peers_shuffled:
            av_set = set(peer.available_pieces)
            pieces_to_request = []

            for piece_id in sorted_needed:
                if piece_id in av_set and piece_id not in requested_pieces:
                    pieces_to_request.append(piece_id)
                    if len(pieces_to_request) >= self.max_requests:
                        break
            
            for piece_id in pieces_to_request:
                start_block = self.pieces[piece_id]
                requests.append(Request(self.id, peer.id, piece_id, start_block))
                requested_pieces.add(piece_id)

            if len(requested_pieces) == len(needed_pieces):
                break
        
        return requests

    def uploads(self, requests, peers, history):
        """
        Implements the reference choking algorithm:
        - Select top (m-1) peers by download rate for regular unchoking
        - Every 3 rounds, randomly select one peer for optimistic unchoking
        - Split bandwidth equally among all m unchoked peers
        
        Edge cases handled:
        - No interested peers (empty requests)
        - Round 0 (no download history)
        - Fewer than m interested peers
        - All peers have zero download rate
        - Empty unchoked list after selection
        """
        round = history.current_round()
        logging.debug("%s again. It's round %d." % (self.id, round))
        
        # Handle case where no one wants our pieces
        if len(requests) == 0:
            logging.debug("No one wants my pieces!")
            
            return []
        
        # Get list of interested peer IDs
        interested_peers = self._get_interested_peer_ids(requests)
        
        # Calculate download rates from recent history
        download_rates = self._get_download_rates(history, round)
        
        # Select top (m-1) peers by download rate for regular unchoking
        regular_unchoked = self._select_unchoked_peers(
            interested_peers, 
            download_rates
        )
        
        # Refresh optimistic peer periodically, or when stale/duplicated.
        should_refresh_optimistic = (
            self._should_update_optimistic(history) or
            self.optimistic_unchoke_peer not in interested_peers or
            self.optimistic_unchoke_peer in regular_unchoked
        )
        if should_refresh_optimistic:
            self.optimistic_unchoke_peer = self._select_optimistic_unchoke(
                interested_peers,
                regular_unchoked
            )
        
        # Combine regular + optimistic for final unchoked list
        unchoked = regular_unchoked.copy()
        if self.optimistic_unchoke_peer is not None and self.optimistic_unchoke_peer not in unchoked:
            unchoked.append(self.optimistic_unchoke_peer)
        
        # EDGE CASE: If no peers can be unchoked (early rounds, no history)
        # Fall back to random selection from interested peers (up to m slots)
        if len(unchoked) == 0:
            logging.debug("No peers selected by algorithm (likely round 0), using random selection")
            num_to_unchoke = min(self.num_upload_slots, len(interested_peers))
            unchoked = random.sample(interested_peers, num_to_unchoke)
            self.optimistic_unchoke_peer = None  # No specific optimistic in this case
        
        # Store for tracking
        self.unchoked_peers = unchoked
        
        logging.debug("Unchoked peers: %s (regular: %s, optimistic: %s)" % 
                     (unchoked, regular_unchoked, self.optimistic_unchoke_peer))
        
        # Split bandwidth equally among all unchoked peers
        bws = even_split(self.up_bw, len(unchoked))
        
        # Create Upload objects
        uploads = [Upload(self.id, peer_id, bw)
                   for (peer_id, bw) in zip(unchoked, bws)]
        
        return uploads
    
    # =========================================================================
    # Helper methods
    # =========================================================================
    
   
    
    def _sort_pieces_by_rarity(self, needed_pieces, peers):

        """Calculate how many peers have each piece."""
        rarity_dic = defaultdict(int)
        
        for piece_id in needed_pieces:
            for peer in peers:
                if piece_id in peer.available_pieces:
                    rarity_dic[piece_id] += 1
        
        
        """Sort pieces by rarity (least common first), break ties randomly."""
        pieces_with_rarity = []
        for piece_id in needed_pieces:
            rarity = rarity_dic.get(piece_id, float('inf'))
            pieces_with_rarity.append((piece_id, rarity, random.random()))
        
        # Sort by rarity (ascending), then random for tie-breaking
        pieces_with_rarity.sort(key=lambda x: (x[1], x[2]))
        
        return [piece_id for piece_id, _, _ in pieces_with_rarity]
    
    def _get_download_rates(self, history, current_round, window_rounds=2):
        """Calculate average download rate from each peer over last 2 rounds (20 seconds)."""
        rates = defaultdict(float)
        
        if current_round == 0:
            return dict(rates)
        
        # Look back over last 2 rounds
        start_round = max(0, current_round - window_rounds)
        total_blocks = defaultdict(int)
        rounds_considered = history.downloads[start_round:current_round]
        
        # Sum blocks received from each peer
        for round_downloads in rounds_considered:
            for download in round_downloads:
                total_blocks[download.from_id] += download.blocks
        
        # Calculate rate (blocks per second)
        # Assuming 10 seconds per round
        total_seconds = max(1, len(rounds_considered)) * 10
        
        for peer_id, blocks in total_blocks.items():
            if total_seconds > 0:
                rates[peer_id] = blocks / total_seconds
        
        return dict(rates)
    
    def _select_unchoked_peers(self, interested_peers, download_rates):
        """Select top (m-1) peers by download rate."""
        peers_with_rates = [
            (peer_id, download_rates.get(peer_id, 0.0), random.random())
            for peer_id in interested_peers
        ]
        
        # Sort by rate (descending), break ties randomly
        peers_with_rates.sort(key=lambda x: (x[1], x[2]), reverse=True)
        
        # Select top (m-1) peers
        num_to_select = min(self.num_upload_slots - 1, len(peers_with_rates))
        selected = [peer_id for peer_id, rate, _ in peers_with_rates[:num_to_select]]
        
        return selected
    
    def _select_optimistic_unchoke(self, interested_peers, currently_unchoked):
        """Select random peer for optimistic unchoking (excluding already unchoked)."""
        unchoked_set = set(currently_unchoked)
        candidates = [peer_id for peer_id in interested_peers 
                      if peer_id not in unchoked_set]
        
        if len(candidates) == 0:
            return None
        
        return random.choice(candidates)
    
    def _should_update_optimistic(self,history):
        """Check if we should select new optimistic peer (every 3 rounds)."""
        return history.current_round() % 3 == 0 
    
    def _get_interested_peer_ids(self, requests):
        """Extract unique peer IDs from requests list."""
        return list(set([request.requester_id for request in requests]))
