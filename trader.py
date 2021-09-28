import time
from typing import Optional

from loguru import logger
from web3 import Web3
from web3.contract import ContractFunction, Contract
from web3.types import Wei, TxParams


class Trader:
    max_approval_hex = f"0x{64 * 'f'}"
    max_approval_int = int(max_approval_hex, 16)
    max_approval_check_hex = f"0x{15 * '0'}{49 * 'f'}"
    max_approval_check_int = int(max_approval_check_hex, 16)

    def __init__(self, web3: Web3, router_address, router_abi, token_contract: Contract, token_address):
        self.web3 = web3
        self.router_address = router_address
        self.router_contract = self.web3.eth.contract(address=router_address, abi=router_abi)
        self.token_contract = token_contract
        self.token_address = token_address
        self.symbol = self.token_contract.functions.symbol().call()
        self.decimals = 10 ** self.token_contract.functions.decimals().call()
        self.wbnb_address = self.router_contract.functions.WETH().call()

    def approve(self, wallet, private_key) -> None:
        """Give an router max approval of a token."""
        approve_function = self.token_contract.functions.approve(self.router_address, self.max_approval_int)
        logger.info(f"Approving {self.symbol}...")
        tx = self._build_and_send_tx(approve_function, wallet, private_key)
        receipt = self.web3.eth.waitForTransactionReceipt(tx, timeout=6000)
        logger.info(f'Approved: {receipt}')
        # Add extra sleep to let tx propagate correctly
        time.sleep(1)

    def _is_approved(self, wallet) -> bool:
        """Check to see if the exchange and token is approved."""
        amount = self.token_contract.functions.allowance(wallet, self.router_address).call()
        if amount >= self.max_approval_check_int:
            return True
        return False

    def get_bnb_balance(self, wallet, in_ether: bool = False):
        """Get the balance of BNB in a wallet."""
        balance = self.web3.eth.getBalance(wallet)
        return Web3.fromWei(balance, 'ether') if in_ether else balance

    def get_token_balance(self, wallet, formatted: bool = False) -> int:
        """Get the balance of a token in a wallet."""
        balance: int = self.token_contract.functions.balanceOf(wallet).call()
        return balance / self.decimals if formatted else balance

    @staticmethod
    def _deadline() -> int:
        """Get a predefined deadline. 10min by default (same as the Uniswap SDK)."""
        return int(time.time()) + 10 * 60

    def can_buy(self, bnb, wallet=None, tx_fee=None) -> bool:
        if not tx_fee and wallet:
            buy_function = self._swap_eth_for_tokens(wallet)
            gas_limit = self.estimate_gas(buy_function, wallet, bnb)
            gas_price = self.web3.eth.gas_price
            tx_fee = self._calc_tx_fee(gas_limit, gas_price)
        return bnb - (tx_fee * 6) > 0

    def can_sell(self, wallet, amount):
        sell_function = self._swap_tokens_for_eth(wallet, amount)
        gas_limit = self.estimate_gas(sell_function, wallet)
        gas_price = self.web3.eth.gas_price
        tx_fee = self._calc_tx_fee(gas_limit, gas_price)
        bnb_balance = self.get_bnb_balance(wallet)
        print(tx_fee)
        return bnb_balance - tx_fee > 0

    @staticmethod
    def estimate_gas(function: ContractFunction, address_from, value: Wei = Wei(0)) -> Wei:
        return Wei(function.estimateGas({'from': address_from, 'value': value}) + 20000)

    def _get_tx_params(self, function: ContractFunction, address_from: str, value: Wei = Wei(0)) -> TxParams:
        """Get generic transaction parameters."""
        gas_limit = self.estimate_gas(function, address_from, value)
        gas_price = self.web3.eth.gas_price
        if value > 0:
            tx_fee = self._calc_tx_fee(gas_limit, gas_price)
            if not self.can_buy(value, tx_fee=tx_fee):
                raise Exception('BNB balance is insufficient for [BUY]')
            value -= tx_fee * 6
        return {
            'from': address_from,
            'value': value,
            "gas": gas_limit,
            'gasPrice': gas_price,
            "nonce": self.web3.eth.getTransactionCount(address_from)
        }

    @staticmethod
    def _calc_tx_fee(gas_limit: Wei, gas_price: Wei):
        return gas_limit * gas_price

    @staticmethod
    def wei_to_eth(wei):
        return Web3.fromWei(wei, 'ether')

    def _build_and_send_tx(self, function: ContractFunction, address_from, private_key,
                           tx_params: Optional[TxParams] = None):
        """Build and send a transaction."""
        if not tx_params:
            tx_params = self._get_tx_params(function, address_from)
        transaction = function.buildTransaction(tx_params)
        signed_txn = self.web3.eth.account.sign_transaction(
            transaction, private_key=private_key
        )
        return self.web3.eth.sendRawTransaction(signed_txn.rawTransaction)

    def _swap_eth_for_tokens(self, wallet):
        return self.router_contract.functions.swapExactETHForTokens(
            0,
            [self.wbnb_address, self.token_address],
            wallet,
            self._deadline()
        )

    def buy(self, wallet, private_key, bnb_amount) -> dict:
        try:
            buy_function = self._swap_eth_for_tokens(wallet)
            balance_before = self.get_token_balance(wallet, formatted=True)
            before = time.time()
            tx_params = self._get_tx_params(buy_function, wallet, bnb_amount)
            bnb_used = self.wei_to_eth(tx_params["value"])
            logger.info(f'[BUY] using {wallet} {self.symbol} token for {bnb_used} BNB')
            tx_hash = self._build_and_send_tx(buy_function, wallet, private_key, tx_params)
            tx_hash_hex = str(self.web3.toHex(tx_hash))
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash)

            after = time.time()
            after_balance = self.get_token_balance(wallet, formatted=True)
            after_bnb_balance = self.get_bnb_balance(wallet, in_ether=True)
            if receipt.status == 1:
                logger.info(
                    f"[BUY] Successfully bought {after_balance - balance_before} {self.symbol} for {bnb_used} BNB - TX HASH: {tx_hash_hex}")
                logger.info(f'Time spend: {after - before} secs')
                tx_fee = self.wei_to_eth(bnb_amount) - (after_bnb_balance + bnb_used)
                logger.info(f'Tx fee: {tx_fee}')
                logger.info(f'Current BNB balance: {after_bnb_balance}')
                logger.info(f'Current {self.symbol} balance: {after_balance}\n')
            else:
                logger.error(f'Transaction failed: {receipt}')
            return {'tx': tx_hash_hex, 'status': receipt.status, 'bnb': bnb_used,
                    'amount': after_balance - balance_before}
        except Exception as e:
            logger.exception(f"[ERROR] while [BUY]: {e}")

    def _swap_tokens_for_eth(self, wallet, amount):
        return self.router_contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount,
            0,
            [self.token_address, self.wbnb_address],
            wallet,
            self._deadline()
        )

    def sell(self, wallet, private_key, amount):
        try:
            if amount <= 0:
                raise Exception('Invalid token amount or token balance is insufficient')
            if not self._is_approved(wallet):
                self.approve(wallet, private_key)
            logger.info(f'[SELL] using {wallet} {amount / self.decimals} {self.symbol} tokens for BNB')
            sell_function = self._swap_tokens_for_eth(wallet, amount)

            before_bnb_balance = self.get_bnb_balance(wallet, in_ether=True)
            before = time.time()
            tx_params = self._get_tx_params(sell_function, wallet)
            tx_hash = self._build_and_send_tx(sell_function, wallet, private_key, tx_params)
            tx_hash_hex = str(self.web3.toHex(tx_hash))
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash)

            after = time.time()
            after_balance = self.get_token_balance(wallet, formatted=True)
            after_bnb_balance = self.get_bnb_balance(wallet, in_ether=True)
            if receipt.status == 1:
                logger.info(
                    f"[SELL] Successfully sold {amount / self.decimals} {self.symbol} for {after_bnb_balance - before_bnb_balance} BNB - TX HASH: {tx_hash_hex}")
                logger.info(f'Time spend: {after - before} secs')
                logger.info(f'Current BNB balance: {after_bnb_balance}')
                logger.info(f'Current {self.symbol} balance: {after_balance}\n')
            else:
                logger.error(f'Transaction failed: {receipt}')
            return {'tx': tx_hash_hex, 'status': receipt.status, 'bnb': after_bnb_balance - before_bnb_balance,
                    'amount': amount / self.decimals}

        except Exception as e:
            logger.error(f"[ERROR] while [SELL]: {e}")
