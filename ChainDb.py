
#
# ChainDb.py - Bitcoin blockchain database
#
# Distributed under the MIT/X11 software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
#

import string
import cStringIO
import gdbm
import os
import time
from decimal import Decimal
from Cache import Cache
from bitcoin.serialize import *
from bitcoin.core import *
from bitcoin.coredefs import COIN
from bitcoin.scripteval import VerifySignature


def tx_blk_cmp(a, b):
	if a.dFeePerKB != b.dFeePerKB:
		return int(a.dFeePerKB - b.dFeePerKB)
	return int(a.dPriority - b.dPriority)

def block_value(height, fees):
	subsidy = 50 * COIN
	subsidy >>= (height / 210000)
	return subsidy + fees

class TxIdx(object):
	def __init__(self, blkhash=0L, spentmask=0L):
		self.blkhash = blkhash
		self.spentmask = spentmask


class BlkMeta(object):
	def __init__(self):
		self.height = -1
		self.work = 0L

	def deserialize(self, s):
		l = s.split()
		if len(l) < 2:
			raise RuntimeError
		self.height = int(l[0])
		self.work = long(l[1], 16)

	def serialize(self):
		r = str(self.height) + ' ' + hex(self.work)
		return r

	def __repr__(self):
		return "BlkMeta(height %d, work %x)" % (self.height, self.work)


class HeightIdx(object):
	def __init__(self):
		self.blocks = []

	def deserialize(self, s):
		self.blocks = []
		l = s.split()
		for hashstr in l:
			hash = long(hashstr, 16)
			self.blocks.append(hash)

	def serialize(self):
		l = []
		for blkhash in self.blocks:
			l.append(hex(blkhash))
		return ' '.join(l)

	def __repr__(self):
		return "HeightIdx(blocks=%s)" % (self.serialize(),)


class ChainDb(object):
	def __init__(self, datadir, log, mempool, netmagic, readonly=False,
		     fast_dbm=False):
		self.log = log
		self.mempool = mempool
		self.readonly = readonly
		self.netmagic = netmagic
		self.fast_dbm = fast_dbm
		self.blk_cache = Cache(750)
		self.orphans = {}
		self.orphan_deps = {}
		if readonly:
			mode_str = 'r'
		else:
			mode_str = 'c'
			if fast_dbm:
				self.log.write("Opening database in fast mode")
				mode_str += 'f'
		self.misc = gdbm.open(datadir + '/misc.dat', mode_str)
		self.blocks = gdbm.open(datadir + '/blocks.dat', mode_str)
		self.height = gdbm.open(datadir + '/height.dat', mode_str)
		self.blkmeta = gdbm.open(datadir + '/blkmeta.dat', mode_str)
		self.tx = gdbm.open(datadir + '/tx.dat', mode_str)

		if 'height' not in self.misc:
			self.log.write("INITIALIZING EMPTY BLOCKCHAIN DATABASE")
			self.misc['height'] = str(-1)
			self.misc['msg_start'] = self.netmagic.msg_start
			self.misc['tophash'] = ser_uint256(0L)
			self.misc['total_work'] = hex(0L)

		if 'msg_start' not in self.misc or (self.misc['msg_start'] != self.netmagic.msg_start):
			self.log.write("Database magic number mismatch. Data corruption or incorrect network?")
			raise RuntimeError

	def dbsync(self):
		self.misc.sync()
		self.blocks.sync()
		self.height.sync()
		self.blkmeta.sync()
		self.tx.sync()

	def puttxidx(self, txhash, txidx):
		ser_txhash = ser_uint256(txhash)

		if ser_txhash in self.tx:
			old_txidx = self.gettxidx(txhash)
			self.log.write("WARNING: overwriting duplicate TX %064x, height %d, oldblk %064x, oldspent %x, newblk %064x" % (txhash, self.getheight(), old_txidx.blkhash, old_txidx.spentmask, txidx.blkhash))

		self.tx[ser_txhash] = (hex(txidx.blkhash) + ' ' +
				       hex(txidx.spentmask))

		return True

	def gettxidx(self, txhash):
		ser_txhash = ser_uint256(txhash)
		if ser_txhash not in self.tx:
			return None

		ser_value = self.tx[ser_txhash]
		pos = string.find(ser_value, ' ')

		txidx = TxIdx()
		txidx.blkhash = long(ser_value[:pos], 16)
		txidx.spentmask = long(ser_value[pos+1:], 16)

		return txidx

	def gettx(self, txhash):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return None

		block = self.getblock(txidx.blkhash)
		for tx in block.vtx:
			tx.calc_sha256()
			if tx.sha256 == txhash:
				return tx

		self.log.write("ERROR: Missing TX %064x in block %064x" % (txhash, txidx.blkhash))
		return None

	def haveblock(self, blkhash, checkorphans):
		if self.blk_cache.exists(blkhash):
			return True
		if checkorphans and blkhash in self.orphans:
			return True
		ser_hash = ser_uint256(blkhash)
		if ser_hash in self.blocks:
			return True
		return False

	def have_prevblock(self, block):
		if self.getheight() < 0 and block.sha256 == self.netmagic.block0:
			return True
		if self.haveblock(block.hashPrevBlock, False):
			return True
		return False

	def getblock(self, blkhash):
		block = self.blk_cache.get(blkhash)
		if block is not None:
			return block

		ser_hash = ser_uint256(blkhash)
		if ser_hash not in self.blocks:
			return None

		f = cStringIO.StringIO(self.blocks[ser_hash])
		block = CBlock()
		block.deserialize(f)

		self.blk_cache.put(blkhash, block)

		return block

	def spend_txout(self, txhash, n_idx):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return False

		txidx.spentmask |= (1L << n_idx)
		self.puttxidx(txhash, txidx)

		return True

	def clear_txout(self, txhash, n_idx):
		txidx = self.gettxidx(txhash)
		if txidx is None:
			return False

		txidx.spentmask &= ~(1L << n_idx)
		self.puttxidx(txhash, txidx)

		return True

	def unique_outpts(self, block):
		outpts = {}
		txmap = {}
		for tx in block.vtx:
			if tx.is_coinbase:
				continue
			txmap[tx.sha256] = tx
			for txin in tx.vin:
				v = (txin.prevout.hash, txin.prevout.n)
				if v in outs:
					return None

				outpts[v] = False

		return (outpts, txmap)

	def spent_outpts(self, block):
		# list of outpoints this block wants to spend
		l = self.unique_outpts(block)
		if l is None:
			return None
		outpts = l[0]
		txmap = l[1]
		spendlist = {}

		# pass 1: if outpoint in db, make sure it is unspent
		for k in outpts.iterkeys():
			txidx = self.gettxidx(k[0])
			if txidx is None:
				continue

			if k[1] > 100000:	# outpoint index sanity check
				return None

			if txidx.spentmask & (1L << k[1]):
				return None

			outpts[k] = True	# skip in pass 2

		# pass 2: remaining outpoints must exist in this block
		for k, v in outpts.iteritems():
			if v:
				continue

			if k[0] not in txmap:	# validate txout hash
				return None

			tx = txmap[k[0]]	# validate txout index (n)
			if k[1] >= len(tx.vout):
				return None

			# outpts[k] = True	# not strictly necessary

		return outpts.keys()

	def tx_signed(self, tx, block, check_mempool):
		tx.calc_sha256()

		for i in xrange(len(tx.vin)):
			txin = tx.vin[i]

			# search database for dependent TX
			txfrom = self.gettx(txin.prevout.hash)

			# search block for dependent TX
			if txfrom is None and block is not None:
				for blktx in block.vtx:
					blktx.calc_sha256()
					if blktx.sha256 == txin.prevout.hash:
						txfrom = blktx
						break

			# search mempool for dependent TX
			if txfrom is None and check_mempool:
				try:
					txfrom = self.mempool.pool[txin.prevout.hash]
				except:
					self.log.write("TX %064x/%d no-dep %064x" %
							(tx.sha256, i,
							 txin.prevout.hash))
					return False
			if txfrom is None:
				self.log.write("TX %064x/%d no-dep %064x" %
						(tx.sha256, i,
						 txin.prevout.hash))
				return False

			if not VerifySignature(txfrom, tx, i, 0):
				self.log.write("TX %064x/%d sigfail" %
						(tx.sha256, i))
				return False

		return True

	def tx_connected(self, tx):
		if not tx.is_valid():
			return False

		block = CBlock()
		block.vtx.append(tx)

		outpts = self.spent_outpts(block)
		if outpts is None:
			return False

		return True

	def connect_block(self, ser_hash, block, blkmeta):
		# check TX connectivity
		outpts = self.spent_outpts(block)
		if outpts is None:
			self.log.write("Unconnectable block %064x" % (block.sha256, ))
			return False

		# verify script signatures
		for tx in block.vtx:
			tx.calc_sha256()

			if tx.is_coinbase():
				continue

			if not self.tx_signed(tx, block, False):
				self.log.write("Invalid signature in block %064x" % (block.sha256, ))
				return False

		# update database pointers for best chain
		self.misc['total_work'] = hex(blkmeta.work)
		self.misc['height'] = str(blkmeta.height)
		self.misc['tophash'] = ser_hash

		self.log.write("ChainDb: height %d, block %064x" % (
				blkmeta.height, block.sha256))

		# all TX's in block are connectable; index
		neverseen = 0
		for tx in block.vtx:
			if not self.mempool.remove(tx.sha256):
				neverseen += 1

			txidx = TxIdx(block.sha256)
			if not self.puttxidx(tx.sha256, txidx):
				self.log.write("TxIndex failed %064x" % (tx.sha256,))
				return False

		self.log.write("MemPool: blk.vtx.sz %d, neverseen %d, poolsz %d" % (len(block.vtx), neverseen, self.mempool.size()))

		# mark deps as spent
		for outpt in outpts:
			self.spend_txout(outpt[0], outpt[1])

		return True

	def disconnect_block(self, block):
		ser_prevhash = ser_uint256(block.hashPrevBlock)
		prevmeta = BlkMeta()
		prevmeta.deserialize(self.blkmeta[ser_prevhash])

		tup = self.unique_outpts(block)
		if tup is None:
			return False

		outpts = tup[0]

		# mark deps as unspent
		for outpt in outpts:
			self.clear_txout(outpt[0], outpt[1])

		# update tx index and memory pool
		for tx in block.vtx:
			tx.calc_sha256()
			ser_hash = ser_uint256(tx.sha256)
			if ser_hash in self.tx:
				del self.tx[ser_hash]

			if not tx.is_coinbase():
				self.mempool.add(tx)

		# update database pointers for best chain
		self.misc['total_work'] = hex(prevmeta.work)
		self.misc['height'] = str(prevmeta.height)
		self.misc['tophash'] = ser_prevhash

		self.log.write("ChainDb(disconn): height %d, block %064x" % (
				prevmeta.height, block.hashPrevBlock))

		return True

	def getblockmeta(self, blkhash):
		ser_hash = ser_uint256(blkhash)
		if ser_hash not in self.blkmeta:
			return None

		meta = BlkMeta()
		meta.deserialize(self.blkmeta[ser_hash])

		return meta
	
	def getblockheight(self, blkhash):
		meta = self.getblockmeta(blkhash)
		if meta is None:
			return -1

		return meta.height

	def reorganize(self, new_best_blkhash):
		self.log.write("REORGANIZE")

		conn = []
		disconn = []

		old_best_blkhash = self.gettophash()
		fork = old_best_blkhash
		longer = new_best_blkhash
		while fork != longer:
			while (self.getblockheight(longer) >
			       self.getblockheight(fork)):
				block = self.getblock(longer)
				block.calc_sha256()
				conn.append(block)

				longer = block.hashPrevBlock
				if longer == 0:
					return False

			if fork == longer:
				break

			block = self.getblock(fork)
			block.calc_sha256()
			disconn.append(block)

			fork = block.hashPrevBlock
			if fork == 0:
				return False

		self.log.write("REORG disconnecting top hash %064x" % (old_best_blkhash,))
		self.log.write("REORG connecting new top hash %064x" % (new_best_blkhash,))
		self.log.write("REORG chain union point %064x" % (fork,))
		self.log.write("REORG disconnecting %d blocks, connecting %d blocks" % (len(disconn), len(conn)))

		for block in disconn:
			if not self.disconnect_block(block):
				return False

		for block in conn:
			if not self.connect_block(ser_uint256(block.sha256),
				  block, self.getblockmeta(block.sha256)):
				return False

		self.log.write("REORGANIZE DONE")
		return True

	def set_best_chain(self, ser_prevhash, ser_hash, block, blkmeta):
		# the easy case, extending current best chain
		if (blkmeta.height == 0 or
		    self.misc['tophash'] == ser_prevhash):
			return self.connect_block(ser_hash, block, blkmeta)

		# switching from current chain to another, stronger chain
		return self.reorganize(block.sha256)

	def putoneblock(self, block):
		block.calc_sha256()

		if not block.is_valid():
			self.log.write("Invalid block %064x" % (block.sha256, ))
			return False

		if not self.have_prevblock(block):
			self.orphans[block.sha256] = True
			self.orphan_deps[block.hashPrevBlock] = block
			self.log.write("Orphan block %064x (%d orphans)" % (block.sha256, len(self.orphan_deps)))
			return False

		top_height = self.getheight()
		top_work = long(self.misc['total_work'], 16)

		# read metadata for previous block
		prevmeta = BlkMeta()
		if top_height >= 0:
			ser_prevhash = ser_uint256(block.hashPrevBlock)
			prevmeta.deserialize(self.blkmeta[ser_prevhash])
		else:
			ser_prevhash = ''

		# store raw block data
		ser_hash = ser_uint256(block.sha256)
		self.blocks[ser_hash] = block.serialize()

		# store metadata related to this block
		blkmeta = BlkMeta()
		blkmeta.height = prevmeta.height + 1
		blkmeta.work = (prevmeta.work +
				uint256_from_compact(block.nBits))
		self.blkmeta[ser_hash] = blkmeta.serialize()

		# store list of blocks at this height
		heightidx = HeightIdx()
		heightstr = str(blkmeta.height)
		if heightstr in self.height:
			heightidx.deserialize(self.height[heightstr])
		heightidx.blocks.append(block.sha256)
		self.height[heightstr] = heightidx.serialize()

		# if chain is not best chain, proceed no further
		if (blkmeta.work <= top_work):
			self.log.write("ChainDb: height %d (weak), block %064x" % (blkmeta.height, block.sha256))
			return True

		# update global chain pointers
		if not self.set_best_chain(ser_prevhash, ser_hash,
					   block, blkmeta):
			return False

		if self.fast_dbm and blkmeta.height % 10000 == 0:
			self.dbsync()

		return True

	def putblock(self, block):
		block.calc_sha256()
		if self.haveblock(block.sha256, True):
			self.log.write("Duplicate block %064x submitted" % (block.sha256, ))
			return False

		if not self.putoneblock(block):
			return False

		blkhash = block.sha256
		while blkhash in self.orphan_deps:
			block = self.orphan_deps[blkhash]
			if not self.putoneblock(block):
				return True

			del self.orphan_deps[blkhash]
			del self.orphans[block.sha256]

			blkhash = block.sha256

		return True

	def locate(self, locator):
		for hash in locator.vHave:
			ser_hash = ser_uint256(hash)
			if ser_hash in self.blkmeta:
				blkmeta = BlkMeta()
				blkmeta.deserialize(self.blkmeta[ser_hash])
				return blkmeta
		return 0

	def getheight(self):
		return int(self.misc['height'])

	def gettophash(self):
		return uint256_from_str(self.misc['tophash'])

	def loadfile(self, filename):
		fd = os.open(filename, os.O_RDONLY)
		self.log.write("IMPORTING DATA FROM " + filename)
		buf = ''
		wanted = 4096
		while True:
			if wanted > 0:
				if wanted < 4096:
					wanted = 4096
				s = os.read(fd, wanted)
				if len(s) == 0:
					break

				buf += s
				wanted = 0

			buflen = len(buf)
			startpos = string.find(buf, self.netmagic.msg_start)
			if startpos < 0:
				wanted = 8
				continue

			sizepos = startpos + 4
			blkpos = startpos + 8
			if blkpos > buflen:
				wanted = 8
				continue

			blksize = struct.unpack("<i", buf[sizepos:blkpos])[0]
			if (blkpos + blksize) > buflen:
				wanted = 8 + blksize
				continue

			ser_blk = buf[blkpos:blkpos+blksize]
			buf = buf[blkpos+blksize:]

			f = cStringIO.StringIO(ser_blk)
			block = CBlock()
			block.deserialize(f)

			self.putblock(block)

	def newblock_txs(self):
		txlist = []
		for tx in self.mempool.pool.itervalues():

			# query finalized, non-coinbase mempool tx's
			if tx.is_coinbase() or not tx.is_final():
				continue

			# iterate through inputs, calculate total input value
			valid = True
			nValueIn = 0
			nValueOut = 0
			dPriority = Decimal(0)

			for tin in tx.vin:
				in_tx = self.gettx(tin.prevout.hash)
				if (in_tx is None or
				    tin.prevout.n >= len(in_tx.vout)):
					valid = False
				else:
					v = in_tx.vout[tin.prevout.n].nValue
					nValueIn += v
					dPriority += Decimal(v * 1)

			if not valid:
				continue

			# iterate through outputs, calculate total output value
			for txout in tx.vout:
				nValueOut += txout.nValue

			# calculate fees paid, if any
			tx.nFeesPaid = nValueIn - nValueOut
			if tx.nFeesPaid < 0:
				continue

			# calculate fee-per-KB and priority
			tx.ser_size = len(tx.serialize())

			dPriority /= Decimal(tx.ser_size)

			tx.dFeePerKB = (Decimal(tx.nFeesPaid) /
					(Decimal(tx.ser_size) / Decimal(1000)))
			if tx.dFeePerKB < Decimal(50000):
				tx.dFeePerKB = Decimal(0)
			tx.dPriority = dPriority

			txlist.append(tx)

		# sort list by fee-per-kb, then priority
		sorted_txlist = sorted(txlist, cmp=tx_blk_cmp, reverse=True)

		# build final list of transactions.  thanks to sort
		# order above, we add TX's to the block in the
		# highest-fee-first order.  free transactions are
		# then appended in order of priority, until
		# free_bytes is exhausted.
		txlist = []
		txlist_bytes = 0
		free_bytes = 50000
		while len(sorted_txlist) > 0:
			tx = sorted_txlist.pop()
			if txlist_bytes + tx.ser_size > (900 * 1000):
				continue

			if tx.dFeePerKB > 0:
				txlist.append(tx)
				txlist_bytes += tx.ser_size
			elif free_bytes >= tx.ser_size:
				txlist.append(tx)
				txlist_bytes += tx.ser_size
				free_bytes -= tx.ser_size
		
		return txlist

	def newblock(self):
		tophash = self.gettophash()
		prevblock = self.getblock(tophash)
		if prevblock is None:
			return None

		# obtain list of candidate transactions for a new block
		total_fees = 0
		txlist = self.newblock_txs()
		for tx in txlist:
			total_fees += tx.nFeesPaid

		#
		# build coinbase
		#
		txin = CTxIn()
		txin.prevout.set_null()
		# FIXME: txin.scriptSig

		txout = CTxOut()
		txout.nValue = block_value(self.getheight(), total_fees)
		# FIXME: txout.scriptPubKey

		coinbase = CTransaction()
		coinbase.vin.append(txin)
		coinbase.vout.append(txout)

		#
		# build block
		#
		block = CBlock()
		block.hashPrevBlock = tophash
		block.nTime = int(time.time())
		block.nBits = prevblock.nBits	# TODO: wrong
		block.vtx.append(coinbase)
		block.vtx.extend(txlist)
		block.hashMerkleRoot = block.calc_merkle()

		return block

