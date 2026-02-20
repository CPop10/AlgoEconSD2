# ChatGPT was used to help guide our implementation. We acknowledge this per the course syllabus' LLM policy and take full responsibility for all written code. 

import random
import logging

from messages import Upload, Request
from util import even_split
from peer import Peer

class CpcwPropShare(Peer):
    def post_init(self):
        print(("PropShare client %s initialized!" % self.id))
        self.optimistic_reserve = 0.10  # 10% for random unblocking

    def requests(self, peers, history):
        needed = lambda i: self.pieces[i] < self.conf.blocks_per_piece
        needed_pieces = list(filter(needed, list(range(len(self.pieces)))))
        np_set = set(needed_pieces)

        requests = []
        random.shuffle(peers) 

        for peer in peers:
            av_set = set(peer.available_pieces)
            isect = av_set.intersection(np_set)
            if not isect:
                continue
            
            # request up to max_requests from this peer
            n = min(self.max_requests, len(isect))
            for piece_id in random.sample(sorted(isect), n):
                start_block = self.pieces[piece_id]
                r = Request(self.id, peer.id, piece_id, start_block)
                requests.append(r)

        return requests

    def uploads(self, requests, peers, history):
        round = history.current_round()
        if not requests:
            return []

        # calculate downloads from last round to determine contribution of requesters
        last_downloads = history.downloads[round - 1] if round > 0 else []
        contribution_map = {}
        for dl in last_downloads:
            contribution_map[dl.from_id] = contribution_map.get(dl.from_id, 0) + dl.blocks

        # find all unique requesters and how much they contributed last round
        requesting_ids = list(set(r.requester_id for r in requests))
        generous_requesters = {}
        others = []
        for p_id in requesting_ids:
            if p_id in contribution_map:
                generous_requesters[p_id] = contribution_map[p_id]
            else:
                others.append(p_id)

        # share 90% of bw across all generous peers
        prop_bw_pool = self.up_bw - self.up_bw // 10
        total_received = sum(generous_requesters.values())
        bw_allocations = {}

        if total_received > 0:
            raw_shares = {
                p_id: blocks * prop_bw_pool / total_received
                for p_id, blocks in generous_requesters.items()
            }
            floor_shares = {p_id: int(s) for p_id, s in raw_shares.items()}
            remainder = prop_bw_pool - sum(floor_shares.values())

            # distribute leftover bandwidth to peers with largest loss
            sorted_by_frac = sorted(
                generous_requesters,
                key=lambda p: raw_shares[p] - floor_shares[p],
                reverse=True
            )
            for i, p_id in enumerate(sorted_by_frac):
                bw_allocations[p_id] = floor_shares[p_id] + (1 if i < remainder else 0)
        else: # no generous peers, split prop_bw_pool evenly among all requesters
            for p_id, bw in zip(requesting_ids, even_split(prop_bw_pool, len(requesting_ids))):
                bw_allocations[p_id] = bw

        # optimistic unchoke w 10% of bw
        opt_bw = self.up_bw // 10
        optimistic_recipient = random.choice(others if others else requesting_ids)
        bw_allocations[optimistic_recipient] = bw_allocations.get(optimistic_recipient, 0) + opt_bw

        return [Upload(self.id, p_id, bw) for p_id, bw in bw_allocations.items() if bw > 0]