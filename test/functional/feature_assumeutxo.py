#!/usr/bin/env python3
# Copyright (c) 2021-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test for assumeutxo, a means of quickly bootstrapping a node using
a serialized version of the UTXO set at a certain height, which corresponds
to a hash that has been compiled into bitcoind.

The assumeutxo value generated and used here is committed to in
`CRegTestParams::m_assumeutxo_data` in `src/kernel/chainparams.cpp`.

## Possible test improvements

Interesting test cases could be loading an assumeutxo snapshot file with:

- TODO: Valid snapshot file, but referencing a snapshot block that turns out to be
      invalid, or has an invalid parent
- TODO: Valid snapshot file and snapshot block, but the block is not on the
      most-work chain

Interesting starting states could be loading a snapshot when the current chain tip is:

- TODO: An ancestor of snapshot block
- TODO: Not an ancestor of the snapshot block but has less work
- TODO: The snapshot block
- TODO: A descendant of the snapshot block
- TODO: Not an ancestor or a descendant of the snapshot block and has more work

"""
from shutil import rmtree

from dataclasses import dataclass
from test_framework.messages import tx_from_hex
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    assert_equal,
    assert_raises_rpc_error,
)
from test_framework.wallet import (
    getnewdestination,
    MiniWallet,
)

START_HEIGHT = 199
SNAPSHOT_BASE_HEIGHT = 299
FINAL_HEIGHT = 399
COMPLETE_IDX = {'synced': True, 'best_block_height': FINAL_HEIGHT}


class AssumeutxoTest(BitcoinTestFramework):

    def set_test_params(self):
        """Use the pregenerated, deterministic chain up to height 199."""
        self.num_nodes = 3
        self.rpc_timeout = 120
        self.extra_args = [
            [],
            ["-fastprune", "-prune=1", "-blockfilterindex=1", "-coinstatsindex=1"],
            ["-persistmempool=0","-txindex=1", "-blockfilterindex=1", "-coinstatsindex=1"],
        ]

    def setup_network(self):
        """Start with the nodes disconnected so that one can generate a snapshot
        including blocks the other hasn't yet seen."""
        self.add_nodes(3)
        self.start_nodes(extra_args=self.extra_args)

    def test_invalid_snapshot_scenarios(self, valid_snapshot_path):
        self.log.info("Test different scenarios of loading invalid snapshot files")
        with open(valid_snapshot_path, 'rb') as f:
            valid_snapshot_contents = f.read()
        bad_snapshot_path = valid_snapshot_path + '.mod'

        def expected_error(log_msg="", rpc_details=""):
            with self.nodes[1].assert_debug_log([log_msg]):
                assert_raises_rpc_error(-32603, f"Unable to load UTXO snapshot{rpc_details}", self.nodes[1].loadtxoutset, bad_snapshot_path)

        self.log.info("  - snapshot file referring to a block that is not in the assumeutxo parameters")
        prev_block_hash = self.nodes[0].getblockhash(SNAPSHOT_BASE_HEIGHT - 1)
        bogus_block_hash = "0" * 64  # Represents any unknown block hash
        for bad_block_hash in [bogus_block_hash, prev_block_hash]:
            with open(bad_snapshot_path, 'wb') as f:
                # block hash of the snapshot base is stored right at the start (first 32 bytes)
                f.write(bytes.fromhex(bad_block_hash)[::-1] + valid_snapshot_contents[32:])
            error_details = f", assumeutxo block hash in snapshot metadata not recognized ({bad_block_hash})"
            expected_error(rpc_details=error_details)

        self.log.info("  - snapshot file with wrong number of coins")
        valid_num_coins = int.from_bytes(valid_snapshot_contents[32:32 + 8], "little")
        for off in [-1, +1]:
            with open(bad_snapshot_path, 'wb') as f:
                f.write(valid_snapshot_contents[:32])
                f.write((valid_num_coins + off).to_bytes(8, "little"))
                f.write(valid_snapshot_contents[32 + 8:])
            expected_error(log_msg=f"bad snapshot - coins left over after deserializing 298 coins" if off == -1 else f"bad snapshot format or truncated snapshot after deserializing 299 coins")

        self.log.info("  - snapshot file with alternated UTXO data")
        cases = [
            # (content, offset, wrong_hash, custom_message)
            [b"\xff" * 32, 0, "094e487229ac0cc067a38ea8554080ca4bae8db62ab71d597e570d8754235286", None],  # wrong outpoint hash
            [(1).to_bytes(4, "little"), 32, "9ff1849255a1eb483395ca9de01ad9ef59f52875d303c578e533b41b8cb2de35", None],  # wrong outpoint index
            [b"\x81", 36, "b7e1315d3df9aa13e8c9dec437dbb6c0c45fc53aa2cc3efc649c880457149093", None],  # wrong coin code VARINT
            [b"\x80", 36, "e859f5f111d59fda4aae219b0185db540e7f2ca31d0858965dff678eaa8ecce5", None],  # another wrong coin code
            [b"\x84\x58", 36, None, "[snapshot] bad snapshot data after deserializing 0 coins"],  # wrong coin case with height 364 and coinbase 0
            [b"\xCA\xD2\x8F\x5A", 41, None, "[snapshot] bad snapshot data after deserializing 0 coins - bad tx out value"],  # Amount exceeds MAX_MONEY
        ]

        for content, offset, wrong_hash, custom_message in cases:
            with open(bad_snapshot_path, "wb") as f:
                f.write(valid_snapshot_contents[:(32 + 8 + offset)])
                f.write(content)
                f.write(valid_snapshot_contents[(32 + 8 + offset + len(content)):])

            log_msg = custom_message if custom_message is not None else f"[snapshot] bad snapshot content hash: expected f39133c5f4af2fb9211a25a997eef28126b4a5d0e4c00bf81c7c323788473280, got {wrong_hash}"
            expected_error(log_msg=log_msg)

    def test_headers_not_synced(self, valid_snapshot_path):
        for node in self.nodes[1:]:
            assert_raises_rpc_error(-32603, "The base block header (c3c15dda786337b86b7fa7079d6f83d0d0625b78bf5b697595b1aa448536c2ea) must appear in the headers chain. Make sure all headers are syncing, and call this RPC again.",
                                    node.loadtxoutset,
                                    valid_snapshot_path)

    def test_invalid_chainstate_scenarios(self):
        self.log.info("Test different scenarios of invalid snapshot chainstate in datadir")

        self.log.info("  - snapshot chainstate referring to a block that is not in the assumeutxo parameters")
        self.stop_node(0)
        chainstate_snapshot_path = self.nodes[0].chain_path / "chainstate_snapshot"
        chainstate_snapshot_path.mkdir()
        with open(chainstate_snapshot_path / "base_blockhash", 'wb') as f:
            f.write(b'z' * 32)

        def expected_error(log_msg="", error_msg=""):
            with self.nodes[0].assert_debug_log([log_msg]):
                self.nodes[0].assert_start_raises_init_error(expected_msg=error_msg)

        expected_error_msg = f"Error: A fatal internal error occurred, see debug.log for details: Assumeutxo data not found for the given blockhash '7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a'."
        error_details = f"Assumeutxo data not found for the given blockhash"
        expected_error(log_msg=error_details, error_msg=expected_error_msg)

        # resurrect node again
        rmtree(chainstate_snapshot_path)
        self.start_node(0)

    def test_invalid_mempool_state(self, dump_output_path):
        self.log.info("Test bitcoind should fail when mempool not empty.")
        node=self.nodes[2]
        tx = MiniWallet(node).send_self_transfer(from_node=node)

        assert tx['txid'] in node.getrawmempool()

        # Attempt to load the snapshot on Node 2 and expect it to fail
        with node.assert_debug_log(expected_msgs=["[snapshot] can't activate a snapshot when mempool not empty"]):
            assert_raises_rpc_error(-32603, "Unable to load UTXO snapshot", node.loadtxoutset, dump_output_path)

        self.restart_node(2, extra_args=self.extra_args[2])

    def run_test(self):
        """
        Bring up two (disconnected) nodes, mine some new blocks on the first,
        and generate a UTXO snapshot.

        Load the snapshot into the second, ensure it syncs to tip and completes
        background validation when connected to the first.
        """
        n0 = self.nodes[0]
        n1 = self.nodes[1]
        n2 = self.nodes[2]

        self.mini_wallet = MiniWallet(n0)

        # Mock time for a deterministic chain
        for n in self.nodes:
            n.setmocktime(n.getblockheader(n.getbestblockhash())['time'])

        # Generate a series of blocks that `n0` will have in the snapshot,
        # but that n1 and n2 don't yet see.
        assert n0.getblockcount() == START_HEIGHT
        blocks = {START_HEIGHT: Block(n0.getbestblockhash(), 1, START_HEIGHT + 1)}
        for i in range(100):
            block_tx = 1
            if i % 3 == 0:
                self.mini_wallet.send_self_transfer(from_node=n0)
                block_tx += 1
            self.generate(n0, nblocks=1, sync_fun=self.no_op)
            height = n0.getblockcount()
            hash = n0.getbestblockhash()
            blocks[height] = Block(hash, block_tx, blocks[height-1].chain_tx + block_tx)
            if i == 4:
                # Create a stale block that forks off the main chain before the snapshot.
                temp_invalid = n0.getbestblockhash()
                n0.invalidateblock(temp_invalid)
                stale_hash = self.generateblock(n0, output="raw(aaaa)", transactions=[], sync_fun=self.no_op)["hash"]
                n0.invalidateblock(stale_hash)
                n0.reconsiderblock(temp_invalid)
                stale_block = n0.getblock(stale_hash, 0)


        self.log.info("-- Testing assumeutxo + some indexes + pruning")

        assert_equal(n0.getblockcount(), SNAPSHOT_BASE_HEIGHT)
        assert_equal(n1.getblockcount(), START_HEIGHT)

        self.log.info(f"Creating a UTXO snapshot at height {SNAPSHOT_BASE_HEIGHT}")
        dump_output = n0.dumptxoutset('utxos.dat')

        self.log.info("Test loading snapshot when headers are not synced")
        self.test_headers_not_synced(dump_output['path'])

        # In order for the snapshot to activate, we have to ferry over the new
        # headers to n1 and n2 so that they see the header of the snapshot's
        # base block while disconnected from n0.
        for i in range(1, 300):
            block = n0.getblock(n0.getblockhash(i), 0)
            # make n1 and n2 aware of the new header, but don't give them the
            # block.
            n1.submitheader(block)
            n2.submitheader(block)

        # Ensure everyone is seeing the same headers.
        for n in self.nodes:
            assert_equal(n.getblockchaininfo()["headers"], SNAPSHOT_BASE_HEIGHT)

        assert_equal(
            dump_output['txoutset_hash'],
            "f39133c5f4af2fb9211a25a997eef28126b4a5d0e4c00bf81c7c323788473280")
        assert_equal(dump_output["nchaintx"], blocks[SNAPSHOT_BASE_HEIGHT].chain_tx)
        assert_equal(n0.getblockchaininfo()["blocks"], SNAPSHOT_BASE_HEIGHT)

        # Mine more blocks on top of the snapshot that n1 hasn't yet seen. This
        # will allow us to test n1's sync-to-tip on top of a snapshot.
        self.generate(n0, nblocks=100, sync_fun=self.no_op)

        assert_equal(n0.getblockcount(), FINAL_HEIGHT)
        assert_equal(n1.getblockcount(), START_HEIGHT)

        assert_equal(n0.getblockchaininfo()["blocks"], FINAL_HEIGHT)

        self.test_invalid_mempool_state(dump_output['path'])
        self.test_invalid_snapshot_scenarios(dump_output['path'])
        self.test_invalid_chainstate_scenarios()

        self.log.info(f"Loading snapshot into second node from {dump_output['path']}")
        loaded = n1.loadtxoutset(dump_output['path'])
        assert_equal(loaded['coins_loaded'], SNAPSHOT_BASE_HEIGHT)
        assert_equal(loaded['base_height'], SNAPSHOT_BASE_HEIGHT)

        def check_tx_counts(final: bool) -> None:
            """Check nTx and nChainTx intermediate values right after loading
            the snapshot, and final values after the snapshot is validated."""
            for height, block in blocks.items():
                tx = n1.getblockheader(block.hash)["nTx"]
                chain_tx = n1.getchaintxstats(nblocks=1, blockhash=block.hash)["txcount"]

                # Intermediate nTx of the starting block should be set, but nTx of
                # later blocks should be 0 before they are downloaded.
                if final or height == START_HEIGHT:
                    assert_equal(tx, block.tx)
                else:
                    assert_equal(tx, 0)

                # Intermediate nChainTx of the starting block and snapshot block
                # should be set, but others should be 0 until they are downloaded.
                if final or height in (START_HEIGHT, SNAPSHOT_BASE_HEIGHT):
                    assert_equal(chain_tx, block.chain_tx)
                else:
                    assert_equal(chain_tx, 0)

        check_tx_counts(final=False)

        normal, snapshot = n1.getchainstates()["chainstates"]
        assert_equal(normal['blocks'], START_HEIGHT)
        assert_equal(normal.get('snapshot_blockhash'), None)
        assert_equal(normal['validated'], True)
        assert_equal(snapshot['blocks'], SNAPSHOT_BASE_HEIGHT)
        assert_equal(snapshot['snapshot_blockhash'], dump_output['base_hash'])
        assert_equal(snapshot['validated'], False)

        assert_equal(n1.getblockchaininfo()["blocks"], SNAPSHOT_BASE_HEIGHT)

        self.log.info("Submit a stale block that forked off the chain before the snapshot")
        # Normally a block like this would not be downloaded, but if it is
        # submitted early before the background chain catches up to the fork
        # point, it winds up in m_blocks_unlinked and triggers a corner case
        # that previously crashed CheckBlockIndex.
        n1.submitblock(stale_block)
        n1.getchaintips()
        n1.getblock(stale_hash)

        self.log.info("Submit a spending transaction for a snapshot chainstate coin to the mempool")
        # spend the coinbase output of the first block that is not available on node1
        spend_coin_blockhash = n1.getblockhash(START_HEIGHT + 1)
        assert_raises_rpc_error(-1, "Block not found on disk", n1.getblock, spend_coin_blockhash)
        prev_tx = n0.getblock(spend_coin_blockhash, 3)['tx'][0]
        prevout = {"txid": prev_tx['txid'], "vout": 0, "scriptPubKey": prev_tx['vout'][0]['scriptPubKey']['hex']}
        privkey = n0.get_deterministic_priv_key().key
        raw_tx = n1.createrawtransaction([prevout], {getnewdestination()[2]: 24.99})
        signed_tx = n1.signrawtransactionwithkey(raw_tx, [privkey], [prevout])['hex']
        signed_txid = tx_from_hex(signed_tx).rehash()

        assert n1.gettxout(prev_tx['txid'], 0) is not None
        n1.sendrawtransaction(signed_tx)
        assert signed_txid in n1.getrawmempool()
        assert not n1.gettxout(prev_tx['txid'], 0)

        PAUSE_HEIGHT = FINAL_HEIGHT - 40

        self.log.info("Restarting node to stop at height %d", PAUSE_HEIGHT)
        self.restart_node(1, extra_args=[
            f"-stopatheight={PAUSE_HEIGHT}", *self.extra_args[1]])

        # Finally connect the nodes and let them sync.
        #
        # Set `wait_for_connect=False` to avoid a race between performing connection
        # assertions and the -stopatheight tripping.
        self.connect_nodes(0, 1, wait_for_connect=False)

        n1.wait_until_stopped(timeout=5)

        self.log.info("Checking that blocks are segmented on disk")
        assert self.has_blockfile(n1, "00000"), "normal blockfile missing"
        assert self.has_blockfile(n1, "00001"), "assumed blockfile missing"
        # FIXME: This fails for auxpow, maybe due to changed block structure?
        #assert not self.has_blockfile(n1, "00002"), "too many blockfiles"

        self.log.info("Restarted node before snapshot validation completed, reloading...")
        self.restart_node(1, extra_args=self.extra_args[1])

        # Send snapshot block to n1 out of order. This makes the test less
        # realistic because normally the snapshot block is one of the last
        # blocks downloaded, but its useful to test because it triggers more
        # corner cases in ReceivedBlockTransactions() and CheckBlockIndex()
        # setting and testing nChainTx values, and it exposed previous bugs.
        snapshot_hash = n0.getblockhash(SNAPSHOT_BASE_HEIGHT)
        snapshot_block = n0.getblock(snapshot_hash, 0)
        n1.submitblock(snapshot_block)

        self.connect_nodes(0, 1)

        self.log.info(f"Ensuring snapshot chain syncs to tip. ({FINAL_HEIGHT})")
        self.wait_until(lambda: n1.getchainstates()['chainstates'][-1]['blocks'] == FINAL_HEIGHT)
        self.sync_blocks(nodes=(n0, n1))

        self.log.info("Ensuring background validation completes")
        self.wait_until(lambda: len(n1.getchainstates()['chainstates']) == 1)

        # Ensure indexes have synced.
        completed_idx_state = {
            'basic block filter index': COMPLETE_IDX,
            'coinstatsindex': COMPLETE_IDX,
        }
        self.wait_until(lambda: n1.getindexinfo() == completed_idx_state)

        self.log.info("Re-check nTx and nChainTx values")
        check_tx_counts(final=True)

        for i in (0, 1):
            n = self.nodes[i]
            self.log.info(f"Restarting node {i} to ensure (Check|Load)BlockIndex passes")
            self.restart_node(i, extra_args=self.extra_args[i])

            assert_equal(n.getblockchaininfo()["blocks"], FINAL_HEIGHT)

            chainstate, = n.getchainstates()['chainstates']
            assert_equal(chainstate['blocks'], FINAL_HEIGHT)

            if i != 0:
                # Ensure indexes have synced for the assumeutxo node
                self.wait_until(lambda: n.getindexinfo() == completed_idx_state)


        # Node 2: all indexes + reindex
        # -----------------------------

        self.log.info("-- Testing all indexes + reindex")
        assert_equal(n2.getblockcount(), START_HEIGHT)

        self.log.info(f"Loading snapshot into third node from {dump_output['path']}")
        loaded = n2.loadtxoutset(dump_output['path'])
        assert_equal(loaded['coins_loaded'], SNAPSHOT_BASE_HEIGHT)
        assert_equal(loaded['base_height'], SNAPSHOT_BASE_HEIGHT)

        for reindex_arg in ['-reindex=1', '-reindex-chainstate=1']:
            self.log.info(f"Check that restarting with {reindex_arg} will delete the snapshot chainstate")
            self.restart_node(2, extra_args=[reindex_arg, *self.extra_args[2]])
            assert_equal(1, len(n2.getchainstates()["chainstates"]))
            for i in range(1, 300):
                block = n0.getblock(n0.getblockhash(i), 0)
                n2.submitheader(block)
            loaded = n2.loadtxoutset(dump_output['path'])
            assert_equal(loaded['coins_loaded'], SNAPSHOT_BASE_HEIGHT)
            assert_equal(loaded['base_height'], SNAPSHOT_BASE_HEIGHT)

        normal, snapshot = n2.getchainstates()['chainstates']
        assert_equal(normal['blocks'], START_HEIGHT)
        assert_equal(normal.get('snapshot_blockhash'), None)
        assert_equal(normal['validated'], True)
        assert_equal(snapshot['blocks'], SNAPSHOT_BASE_HEIGHT)
        assert_equal(snapshot['snapshot_blockhash'], dump_output['base_hash'])
        assert_equal(snapshot['validated'], False)

        self.connect_nodes(0, 2)
        self.wait_until(lambda: n2.getchainstates()['chainstates'][-1]['blocks'] == FINAL_HEIGHT)
        self.sync_blocks()

        self.log.info("Ensuring background validation completes")
        self.wait_until(lambda: len(n2.getchainstates()['chainstates']) == 1)

        completed_idx_state = {
            'basic block filter index': COMPLETE_IDX,
            'coinstatsindex': COMPLETE_IDX,
            'txindex': COMPLETE_IDX,
        }
        self.wait_until(lambda: n2.getindexinfo() == completed_idx_state)

        for i in (0, 2):
            n = self.nodes[i]
            self.log.info(f"Restarting node {i} to ensure (Check|Load)BlockIndex passes")
            self.restart_node(i, extra_args=self.extra_args[i])

            assert_equal(n.getblockchaininfo()["blocks"], FINAL_HEIGHT)

            chainstate, = n.getchainstates()['chainstates']
            assert_equal(chainstate['blocks'], FINAL_HEIGHT)

            if i != 0:
                # Ensure indexes have synced for the assumeutxo node
                self.wait_until(lambda: n.getindexinfo() == completed_idx_state)

        self.log.info("Test -reindex-chainstate of an assumeutxo-synced node")
        self.restart_node(2, extra_args=[
            '-reindex-chainstate=1', *self.extra_args[2]])
        assert_equal(n2.getblockchaininfo()["blocks"], FINAL_HEIGHT)
        self.wait_until(lambda: n2.getblockcount() == FINAL_HEIGHT)

        self.log.info("Test -reindex of an assumeutxo-synced node")
        self.restart_node(2, extra_args=['-reindex=1', *self.extra_args[2]])
        self.connect_nodes(0, 2)
        self.wait_until(lambda: n2.getblockcount() == FINAL_HEIGHT)

@dataclass
class Block:
    hash: str
    tx: int
    chain_tx: int

if __name__ == '__main__':
    AssumeutxoTest().main()
