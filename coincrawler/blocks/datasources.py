import requests
import json
from dateutil import parser as dateutilParser
from bs4 import BeautifulSoup
import re
from coincrawler.blockchain.bitcoin import BitcoinAccess
from coincrawler.blockchain.ethereum import EthereumAccess
from coincrawler.blockchain.monero import MoneroAccess
from coincrawler.utils.network import hardenedRequestsGet
from datetime import datetime

class IDataSource(object):

	def getBlockHeight(self):
		return 0

	def getBlock(self, height):
		return None


class NemNinjaDataSource(IDataSource):

	def getBlockHeight(self):
		return int(json.loads(requests.get("http://chain.nem.ninja/api3/blocks", timeout=10).text)[0]["height"]) - 50

	def getBlock(self, height):
		blockData = json.loads(requests.get("http://chain.nem.ninja/api3/block?height=%s" % height, timeout=10).text)
		blockTimestamp = dateutilParser.parse(blockData["timestamp"])
		txCount = blockData["tx_count"]

		txVolume = 0.0
		fees = 0.0
		response = requests.get("http://chain.nem.ninja/api3/block_transactions?height=%s" % height, timeout=10)
		if response.status_code != 500:
			txData = json.loads(response.text)
			for transfer in txData["transfers"]:
				txVolume += float(transfer["amount"]) / 1000000.0
				fees += float(transfer["fee"]) / 1000000.0
		else:
			print "NemNinja get_transactions API failed on block %d" % height

		return {"height": height, "timestamp": blockTimestamp, "txVolume": txVolume, "txCount": txCount, "fees": fees}


class MainnetDecredOrgDataSource(IDataSource):

	def getBlockHeight(self):
		r =	hardenedRequestsGet("https://mainnet.decred.org/api/status?q=getInfo", timeout=10, jsonResponse=True)
		return r.json()["info"]["blocks"] - 40

	def getBlock(self, height):
		r = hardenedRequestsGet("https://mainnet.decred.org/api/block-index/%s" % height, timeout=10, jsonResponse=True)
		blockHash = r.json()["blockHash"]
		
		r = hardenedRequestsGet("https://mainnet.decred.org/api/block/%s" % blockHash, timeout=10, jsonResponse=True)
		data = r.json()
		
		blockTimestamp = dateutilParser.parse(data['unixtime'])
		generatedCoins = data['reward']
		
		txCount = len(data['tx']) - 1
		txVolume = 0.0
		fees = 0.0
		for txid in data['tx']:
			r = hardenedRequestsGet("https://mainnet.decred.org/api/tx/%s" % txid, timeout=10, jsonResponse=True)
			if r.status_code == 404:
				print "404 CODE FOR TX %s" % txid
				continue
			txData = r.json()
			outputs = {}
			inputs = {}
			sumOutputs = 0.0
			sumInputs = 0.0

			isCoinbase = False
			nInputs = 0
			for inputData in txData['vin']:
				if 'coinbase' in inputData:
					isCoinbase = True
				else:
					r = hardenedRequestsGet("https://mainnet.decred.org/api/tx/%s" % inputData['txid'], timeout=10, jsonResponse=True)
					key = frozenset(r.json()['vout'][inputData['vout']]['scriptPubKey']['addresses'])
					if not key in inputs:
						inputs[key] = 0
					inputs[key] += inputData['amountin']
					sumInputs += inputData['amountin']
				nInputs += 1
				print "inputs processed %d / %d" % (nInputs, len(txData['vin']))
			if isCoinbase:
				continue

			for outputData in txData['vout']:
				sumOutputs += float(outputData['value'])
				if 'addresses' in outputData['scriptPubKey']:
					key = frozenset(outputData['scriptPubKey']['addresses'])
					if not key in outputs:
						outputs[key] = 0.0
					outputs[key] += float(outputData['value'])

			for adrs in outputs.keys():
				inInputs = False
				for adr in adrs:
					for iAdrs in inputs.keys():
						if adr in iAdrs:
							inInputs = True
				if not inInputs:
					txVolume += outputs[adrs]

			if sumInputs + 0.00000000001 < sumOutputs:
				print sumInputs, sumOutputs
				assert(False)
			fees += max(0.0, sumInputs - sumOutputs)

		return {"height": height, "timestamp": blockTimestamp, "generatedCoins": generatedCoins, "txCount": txCount, "txVolume": txVolume, "fees": fees, "difficulty": 0.0}


class BitcoinBlockchainDataSource(IDataSource):

	def __init__(self, host, port, user, password, prefetchCount, useLMDBCache=False, lmdbCachePath="", isPivx=False, maxPrefetchInputs=5000, dropBlocksCount=10):
		self.prefetchCount = prefetchCount
		self.blockchainAccess = BitcoinAccess(host, port, user, password, useLMDBCache, lmdbCachePath, isPivx, maxPrefetchInputs)
		self.steps = 0
		self.networkBlocksCount = self.blockchainAccess.getBlockCount() - dropBlocksCount

	def getBlockHeight(self):
		return self.networkBlocksCount

	def getBlock(self, height):
		if self.prefetchCount > 0 and self.steps % self.prefetchCount == 0:
			maxPrefetchHeight = min(self.networkBlocksCount, height + self.prefetchCount)
			amount = maxPrefetchHeight - height
			if amount > 0:
				self.blockchainAccess.prefetchBlocksInfo(height, amount)
			
		generatedCoins, fees, txVolume, txCount, difficulty, blockTime = self.blockchainAccess.getBlockInfo(height)
		txCount -= 1
		blockTimestamp = datetime.utcfromtimestamp(blockTime)

		self.steps += 1
		
		return {"height": height, "timestamp": blockTimestamp, "txVolume": txVolume, "txCount": txCount, "generatedCoins": generatedCoins, "fees": fees, "difficulty": difficulty}


class EthereumBlockchainDataSource(IDataSource):

	def __init__(self, host, port):
		self.ethereumAccess = EthereumAccess(host, port)
		self.networkBlocksCount = self.ethereumAccess.getBlockCount() - 400

	def getBaseBlockReward(self, height):
		BYZANTIUM_FORK_HEIGHT = 4370000
		if height < BYZANTIUM_FORK_HEIGHT:
			return 5.0
		else:
			return 3.0

	def getBlockHeight(self):
		return self.networkBlocksCount

	def getBlock(self, height):
		blockInfo = self.ethereumAccess.getBlockByHeight(height)
		blockTimestamp = datetime.utcfromtimestamp(int(blockInfo['timestamp'], base=16))
		difficulty = int(blockInfo['difficulty'], base=16)
		txVolume = 0.0
		fees = 0.0
		txCount = len(blockInfo['transactions'])

		receipts = self.ethereumAccess.bulkCall([("eth_getTransactionReceipt", [tx['hash']]) for tx in blockInfo['transactions']]) if txCount > 0 else []

		index = 0
		for tx in blockInfo['transactions']:
			txValue = int(tx['value'], base=16) / 1000000000000000000.0
			txVolume += txValue
			gasUsed = int(receipts[index]['gasUsed'], base=16)
			gasPrice = int(tx['gasPrice'], base=16)
			fee = gasUsed * gasPrice / 1000000000000000000.0
			fees += fee
			index += 1

		baseReward = self.getBaseBlockReward(height)
		generatedCoins = baseReward
		unclesCount = len(blockInfo['uncles'])
		unclesCountReward = unclesCount * baseReward / 32
		generatedCoins += unclesCountReward
		unclesReward = 0.0
		if unclesCount > 0:
			uncles = self.ethereumAccess.bulkCall([("eth_getUncleByBlockNumberAndIndex", [hex(height), hex(i)]) for i in xrange(unclesCount)])
			numbers = [int(uncle['number'], base=16) for uncle in uncles]
			for n in numbers:
				unclesReward += baseReward * (n + 8 - height) / 8
		generatedCoins += unclesReward

		return {"height": height, "timestamp": blockTimestamp, "txVolume": txVolume, "txCount": txCount, "generatedCoins": generatedCoins, "fees": fees, "difficulty": difficulty}


class EthereumClassicBlockchainDataSource(EthereumBlockchainDataSource):

	def getBaseBlockReward(self, height):
		return 5.0


class MoneroBlockchainDataSource(IDataSource):

	def __init__(self, host, port):
		self.moneroAccess = MoneroAccess(host, port)
		self.networkBlocksCount = self.moneroAccess.getBlockCount() - 50

	def getBlockHeight(self):
		return self.networkBlocksCount

	def getBlock(self, height):
		blockHeaderJson = self.moneroAccess.getBlockHeaderByHeight(height)
		difficulty = blockHeaderJson["difficulty"]
		timestamp = blockHeaderJson["timestamp"]
		txCount = 0
		blockJson = self.moneroAccess.getBlockByHeight(height)
		txCount = len(blockJson['tx_hashes'])
		coinbase = self.moneroAccess.getCoinbaseTxSum(height, 1)
		fees = coinbase['fee_amount'] / 1000000000000.0
		generatedCoins = coinbase['emission_amount'] / 1000000000000.0
		return {"height": height, "timestamp": timestamp, "difficulty": difficulty, "generatedCoins": generatedCoins, "fees": fees, "txCount": txCount, "txVolume": 0.0}


