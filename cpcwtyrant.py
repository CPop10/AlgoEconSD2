#!/usr/bin/python

import random
import logging
from collections import defaultdict

from messages import Upload, Request
from peer import Peer

class CpcwTyrant(Peer):
    def post_init(self):
        print(("post_init(): %s here!" % self.id))

        # Estimated upload bandwidth needed to keep each peer reciprocating.
        self.peer_upload_cost = {}
        # Estimated download we can expect from each peer.
        self.peer_download_est = {}
        # Peers we unchoked in the previous round.
        self.last_unchoked = set()

        self.min_upload_bw = 1.0
        self.alpha = 0.9  # decrease required upload when reciprocating
        self.gamma = 1.2  # increase required upload when not reciprocating
        self.optimistic_period = 3
        self.initial_upload_guess = max(self.min_upload_bw, self.up_bw / 4.0)
    
    def requests(self, peers, history):
        """
        Use rarest-first with per-round de-duplication to reduce duplicate asks.
        """
        needed = lambda i: self.pieces[i] < self.conf.blocks_per_piece
        needed_pieces = list(filter(needed, list(range(len(self.pieces)))))

        if len(needed_pieces) == 0:
            return []

        logging.debug("%s here: still need pieces %s" % (self.id, needed_pieces))

        sorted_needed = self.sort_pieces_by_rarity(needed_pieces, peers)

        requests = []
        requested_pieces = set()

        peers_shuffled = peers[:]
        random.shuffle(peers_shuffled)

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
        BitTyrant-style upload selection:
        - Estimate each peer's expected download and required upload.
        - Rank peers by expected_download / required_upload.
        - Allocate bandwidth greedily by this ratio.
        - Periodically add an optimistic peer if spare bandwidth remains.
        """
        current_round = history.current_round()
        logging.debug("%s again. It's round %d." % (self.id, current_round))

        if len(requests) == 0:
            self.last_unchoked = set()
            return []

        interested_peers = self.get_interested_peer_ids(requests)
        self.ensure_peer_state(interested_peers)

        observed_downloads = self.get_last_round_downloads(history)
        self.update_download_estimates(observed_downloads)
        self.update_upload_costs(observed_downloads)

        ranked = self.rank_peers(interested_peers)
        allocations = self.allocate_bandwidth(ranked)
        self.add_optimistic_peer(allocations, interested_peers, current_round)

        if len(allocations) == 0:
            fallback_peer = random.choice(interested_peers)
            allocations[fallback_peer] = int(self.up_bw)

        self.last_unchoked = set(allocations.keys())
        uploads = [Upload(self.id, peer_id, bw) for peer_id, bw in allocations.items()]
        return uploads

    # =========================================================================
    # Helper methods
    # =========================================================================

    def sort_pieces_by_rarity(self, needed_pieces, peers):
        rarity = defaultdict(int)
        for piece_id in needed_pieces:
            for peer in peers:
                if piece_id in peer.available_pieces:
                    rarity[piece_id] += 1

        pieces_with_rarity = []
        for piece_id in needed_pieces:
            pieces_with_rarity.append((piece_id, rarity.get(piece_id, float("inf")), random.random()))

        pieces_with_rarity.sort(key=lambda x: (x[1], x[2]))
        return [piece_id for piece_id, _, _ in pieces_with_rarity]

    def get_interested_peer_ids(self, requests):
        return list(set([request.requester_id for request in requests]))

    def ensure_peer_state(self, peer_ids):
        for peer_id in peer_ids:
            if peer_id not in self.peer_upload_cost:
                self.peer_upload_cost[peer_id] = self.initial_upload_guess
            if peer_id not in self.peer_download_est:
                self.peer_download_est[peer_id] = 1.0

    def get_last_round_downloads(self, history):
        downloads = defaultdict(int)
        if history.current_round() == 0:
            return downloads

        for d in history.downloads[-1]:
            downloads[d.from_id] += d.blocks
        return downloads

    def update_download_estimates(self, observed_downloads):
        # Decay stale estimates to avoid overrating peers who stop reciprocating.
        for peer_id in list(self.peer_download_est.keys()):
            if peer_id not in observed_downloads:
                self.peer_download_est[peer_id] *= 0.8

        # Exponential moving average for smoother per-peer value.
        for peer_id, blocks in observed_downloads.items():
            prev = self.peer_download_est.get(peer_id, float(blocks))
            self.peer_download_est[peer_id] = 0.7 * prev + 0.3 * blocks

    def update_upload_costs(self, observed_downloads):
        for peer_id in self.last_unchoked:
            current = self.peer_upload_cost.get(peer_id, self.initial_upload_guess)
            if observed_downloads.get(peer_id, 0) > 0:
                self.peer_upload_cost[peer_id] = max(self.min_upload_bw, current * self.alpha)
            else:
                self.peer_upload_cost[peer_id] = min(float(self.up_bw), current * self.gamma)

    def rank_peers(self, interested_peers): # score interested peers by expected download / required upload and sort descending
        ranked = []
        for peer_id in interested_peers:
            est_download = self.peer_download_est.get(peer_id, 1.0)
            est_cost = max(self.min_upload_bw, self.peer_upload_cost.get(peer_id, self.initial_upload_guess))
            score = est_download / est_cost
            ranked.append((peer_id, score, est_cost, est_download, random.random()))

        ranked.sort(key=lambda x: (x[1], x[3], x[4]), reverse=True)
        return ranked

    def allocate_bandwidth(self, ranked_peers):
        remaining = int(self.up_bw)
        allocations = {}

        for peer_id, _, est_cost, _, _ in ranked_peers:
            bw = int(max(self.min_upload_bw, round(est_cost)))
            if bw <= remaining:
                allocations[peer_id] = bw
                remaining -= bw
            if remaining <= 0:
                break

        if len(allocations) == 0 and len(ranked_peers) > 0:
            allocations[ranked_peers[0][0]] = int(self.up_bw)

        return allocations

    def add_optimistic_peer(self, allocations, interested_peers, current_round):
        if current_round % self.optimistic_period != 0:
            return

        remaining = int(self.up_bw) - sum(allocations.values())
        if remaining <= 0:
            return

        candidates = [p for p in interested_peers if p not in allocations]
        if len(candidates) == 0:
            return

        peer_id = random.choice(candidates)
        bw = int(
            min(
                remaining,
                max(
                    self.min_upload_bw,
                    round(self.peer_upload_cost.get(peer_id, self.initial_upload_guess))
                ),
            )
        )
        allocations[peer_id] = max(1, bw)
