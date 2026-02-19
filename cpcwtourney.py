import random
import logging
from collections import defaultdict

from messages import Upload, Request
from util import even_split
from peer import Peer

class CpcwTourney(Peer):
    def post_init(self):
        print(("post_init(): %s here!" % self.id))
        
        self.early_progress_cutoff = 0.35 # 35% cutoff for early phase
        self.late_progress_cutoff = 0.85 # 85% cutoff for late phase
        self.endgame_duplicate_limit = 2 # max 2 requests per piece in late phase for aggressive endgame

        # early standard state
        self.num_upload_slots = 4
        self.optimistic_unchoke_peer = None

        # late tyrant state
        self.peer_upload_cost = {}
        self.peer_download_est = {}
        self.last_unchoked_tyrant = set()

        # parameters for each phase of the hybrid strategy
        self.min_upload_bw = 1.0
        self.alpha_mid = 0.9
        self.gamma_mid = 1.2
        self.alpha_late = 0.85
        self.gamma_late = 1.25
        self.optimistic_period_mid = 3
        self.optimistic_period_late = 2
        self.initial_upload_guess = max(self.min_upload_bw, self.up_bw / 4.0)
    
    def requests(self, peers, history):
        # hybrid request policy based on how much has been downloaded so far
        # early-mid: rarest-first with de-duplication to maximize piece diversity and minimize wasted requests
        # late: rarest-first with controlled duplicate requests to speed up completion of remaining pieces, especially if some are very rare

        needed = lambda i: self.pieces[i] < self.conf.blocks_per_piece
        needed_pieces = list(filter(needed, list(range(len(self.pieces)))))

        if len(needed_pieces) == 0:
            return []
        # logging.debug("%s here: still need pieces %s" % (self.id, needed_pieces))
        sorted_needed = self.sort_pieces_by_rarity(needed_pieces, peers)
        phase = self.phase()
        duplicate_limit = self.endgame_duplicate_limit if phase == "late" else 1
        return self.build_requests(peers, sorted_needed, duplicate_limit)

    def uploads(self, requests, peers, history):
        # hybrid request policy based on how much has been downloaded so far
        # early: same tft policy as standard
        # mid: tyrant style bandwidth allocation based on estimated costs and return of uploading to each peer
        # late: more aggressive tyrant

        current_round = history.current_round()
        logging.debug("%s again. It's round %d." % (self.id, current_round))

        if len(requests) == 0:
            self.last_unchoked_tyrant = set()
            return []
        phase = self.phase()
        if phase == "early":
            self.last_unchoked_tyrant = set()
            return self.uploads_std(requests, history)
        return self.uploads_tyrant(requests, history, aggressive=(phase == "late"))

    # -------------- common helpers --------------

    def phase(self):
        progress = self.progress_fraction()
        if progress < self.early_progress_cutoff:
            return "early"
        if progress < self.late_progress_cutoff:
            return "mid"
        return "late"

    def progress_fraction(self):
        completed = sum(1 for blocks in self.pieces if blocks == self.conf.blocks_per_piece)
        return completed / float(self.conf.num_pieces)

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

    def build_requests(self, peers, sorted_needed, duplicate_limit):
        requests = []
        piece_request_counts = defaultdict(int)
        peer_request_counts = defaultdict(int)
        requested_pairs = set()

        # pass 1 requests each piece once if possible, later passes add controlled duplicates.
        for pass_idx in range(duplicate_limit):
            peers_shuffled = peers[:]
            random.shuffle(peers_shuffled)

            for peer in peers_shuffled:
                if peer_request_counts[peer.id] >= self.max_requests:
                    continue

                av_set = set(peer.available_pieces)
                for piece_id in sorted_needed:
                    if piece_id not in av_set:
                        continue
                    if piece_request_counts[piece_id] >= duplicate_limit:
                        continue
                    if piece_request_counts[piece_id] >= (pass_idx + 1):
                        continue

                    pair = (peer.id, piece_id)
                    if pair in requested_pairs:
                        continue

                    start_block = self.pieces[piece_id]
                    requests.append(Request(self.id, peer.id, piece_id, start_block))
                    piece_request_counts[piece_id] += 1
                    peer_request_counts[peer.id] += 1
                    requested_pairs.add(pair)

                    if peer_request_counts[peer.id] >= self.max_requests:
                        break

        return requests

    def get_interested_peer_ids(self, requests):
        return list(set([request.requester_id for request in requests]))

    # -------------- early game standard helpers --------------

    def uploads_std(self, requests, history):
        current_round = history.current_round()
        interested_peers = self.get_interested_peer_ids(requests)
        download_rates = self.get_download_rates(history, current_round)

        regular_unchoked = self.select_std_unchoked_peers(interested_peers, download_rates)

        should_refresh_optimistic = (
            self.should_update_optimistic(current_round) or
            self.optimistic_unchoke_peer not in interested_peers or
            self.optimistic_unchoke_peer in regular_unchoked
        )
        if should_refresh_optimistic:
            self.optimistic_unchoke_peer = self.select_optimistic_unchoke(
                interested_peers, regular_unchoked
            )

        chosen = regular_unchoked[:]
        if self.optimistic_unchoke_peer is not None and self.optimistic_unchoke_peer not in chosen:
            chosen.append(self.optimistic_unchoke_peer)

        if len(chosen) == 0:
            num_to_unchoke = min(self.num_upload_slots, len(interested_peers))
            chosen = random.sample(interested_peers, num_to_unchoke)

        bws = even_split(int(self.up_bw), len(chosen))
        return [Upload(self.id, peer_id, bw) for peer_id, bw in zip(chosen, bws)]

    def get_download_rates(self, history, current_round, window_rounds=2):
        rates = defaultdict(float)
        if current_round == 0:
            return dict(rates)

        start_round = max(0, current_round - window_rounds)
        rounds_considered = history.downloads[start_round:current_round]
        total_blocks = defaultdict(int)

        for round_downloads in rounds_considered:
            for download in round_downloads:
                total_blocks[download.from_id] += download.blocks

        total_seconds = max(1, len(rounds_considered)) * 10
        for peer_id, blocks in total_blocks.items():
            rates[peer_id] = blocks / total_seconds

        return dict(rates)

    def select_std_unchoked_peers(self, interested_peers, download_rates):
        peers_with_rates = [
            (peer_id, download_rates.get(peer_id, 0.0), random.random())
            for peer_id in interested_peers
        ]
        peers_with_rates.sort(key=lambda x: (x[1], x[2]), reverse=True)

        num_to_select = min(self.num_upload_slots - 1, len(peers_with_rates))
        return [peer_id for peer_id, _, _ in peers_with_rates[:num_to_select]]

    def should_update_optimistic(self, current_round):
        return current_round % 3 == 0

    def select_optimistic_unchoke(self, interested_peers, currently_unchoked):
        candidates = [peer_id for peer_id in interested_peers if peer_id not in set(currently_unchoked)]
        if len(candidates) == 0:
            return None
        return random.choice(candidates)

    # -------------- tyrant helpers --------------

    def uploads_tyrant(self, requests, history, aggressive=False):
        current_round = history.current_round()
        interested_peers = self.get_interested_peer_ids(requests)

        self.ensure_tyrant_peer_state(interested_peers)
        observed_downloads = self.get_last_round_downloads(history)
        self.update_download_estimates(observed_downloads, aggressive)

        alpha = self.alpha_late if aggressive else self.alpha_mid
        gamma = self.gamma_late if aggressive else self.gamma_mid
        self.update_upload_costs(observed_downloads, alpha, gamma)

        ranked = self.rank_peers(interested_peers)
        allocations, remaining = self.allocate_bandwidth(ranked)

        optimistic_period = self.optimistic_period_late if aggressive else self.optimistic_period_mid
        remaining = self.add_optimistic_peer(
            allocations, interested_peers, current_round, remaining, optimistic_period
        )

        if len(allocations) > 0 and remaining > 0:
            top_peer = self.top_allocated_peer(ranked, allocations)
            if top_peer is not None:
                allocations[top_peer] += remaining
                remaining = 0

        if len(allocations) == 0:
            fallback_peer = random.choice(interested_peers)
            allocations[fallback_peer] = int(self.up_bw)

        self.last_unchoked_tyrant = set(allocations.keys())
        return [Upload(self.id, peer_id, bw) for peer_id, bw in allocations.items()]

    def ensure_tyrant_peer_state(self, peer_ids):
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

    def update_download_estimates(self, observed_downloads, aggressive):
        decay = 0.75 if aggressive else 0.8
        prev_weight = 0.6 if aggressive else 0.7
        new_weight = 1.0 - prev_weight

        for peer_id in list(self.peer_download_est.keys()):
            if peer_id not in observed_downloads:
                self.peer_download_est[peer_id] *= decay

        for peer_id, blocks in observed_downloads.items():
            prev = self.peer_download_est.get(peer_id, float(blocks))
            self.peer_download_est[peer_id] = prev_weight * prev + new_weight * blocks

    def update_upload_costs(self, observed_downloads, alpha, gamma):
        for peer_id in self.last_unchoked_tyrant:
            current = self.peer_upload_cost.get(peer_id, self.initial_upload_guess)
            if observed_downloads.get(peer_id, 0) > 0:
                self.peer_upload_cost[peer_id] = max(self.min_upload_bw, current * alpha)
            else:
                self.peer_upload_cost[peer_id] = min(float(self.up_bw), current * gamma)

    def rank_peers(self, interested_peers):
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
            bw = int(max(1, round(est_cost)))
            if bw <= remaining:
                allocations[peer_id] = bw
                remaining -= bw
            if remaining <= 0:
                break

        return allocations, remaining

    def add_optimistic_peer(self, allocations, interested_peers, current_round, remaining, period):
        if current_round % period != 0:
            return remaining
        if remaining <= 0:
            return remaining

        candidates = [peer_id for peer_id in interested_peers if peer_id not in allocations]
        if len(candidates) == 0:
            return remaining

        peer_id = random.choice(candidates)
        target_bw = int(max(1, round(self.peer_upload_cost.get(peer_id, self.initial_upload_guess))))
        bw = min(remaining, target_bw)
        allocations[peer_id] = bw
        return remaining - bw

    def top_allocated_peer(self, ranked_peers, allocations):
        for peer_id, _, _, _, _ in ranked_peers:
            if peer_id in allocations:
                return peer_id
        if len(allocations) == 0:
            return None
        return next(iter(allocations))
