import asyncio
import logging
import time
from pathlib import Path

from typing import List, Optional, Tuple, Dict, Callable

from blspy import PrivateKey

from src.util.byte_types import hexstr_to_bytes
from src.util.chech32 import encode_puzzle_hash, decode_puzzle_hash
from src.util.keychain import (
    generate_mnemonic,
    bytes_to_mnemonic,
)
from src.util.path import path_from_root
from src.util.ws_message import create_payload

from src.cmds.init import check_keys
from src.server.outbound_message import NodeType, OutboundMessage, Message, Delivery
from src.simulator.simulator_protocol import FarmNewBlockProtocol
from src.util.ints import uint64, uint32
from src.wallet.trade_record import TradeRecord
from src.wallet.util.backup_utils import get_backup_info
from src.wallet.util.trade_utils import trade_record_to_dict
from src.wallet.util.wallet_types import WalletType
from src.wallet.rl_wallet.rl_wallet import RLWallet
from src.wallet.cc_wallet.cc_wallet import CCWallet
from src.wallet.did_wallet.did_wallet import DIDWallet
from src.wallet.wallet_info import WalletInfo
from src.wallet.wallet_node import WalletNode
from src.types.spend_bundle import SpendBundle
from src.types.mempool_inclusion_status import MempoolInclusionStatus

# Timeout for response from wallet/full node for sending a transaction
TIMEOUT = 30

log = logging.getLogger(__name__)


class WalletRpcApi:
    def __init__(self, wallet_node: WalletNode):
        self.service = wallet_node
        self.service_name = "chia_wallet"

    def get_routes(self) -> Dict[str, Callable]:
        return {
            "/get_wallet_balance": self.get_wallet_balance,
            "/send_transaction": self.send_transaction,
            "/get_next_puzzle_hash": self.get_next_puzzle_hash,
            "/get_transactions": self.get_transactions,
            "/farm_block": self.farm_block,
            "/get_sync_status": self.get_sync_status,
            "/get_height_info": self.get_height_info,
            "/create_new_wallet": self.create_new_wallet,
            "/get_wallets": self.get_wallets,
            "/cc_set_name": self.cc_set_name,
            "/cc_get_name": self.cc_get_name,
            "/cc_spend": self.cc_spend,
            "/cc_get_colour": self.cc_get_colour,
            "did_update_recovery_ids": self.did_update_recovery_ids,
            "did_spend": self.did_spend,
            "did_get_id": self.did_get_id,
            "did_recovery_spend": self.did_recovery_spend,
            "did_create_attest": self.did_create_attest,
            "/create_offer_for_ids": self.create_offer_for_ids,
            "/get_discrepancies_for_offer": self.get_discrepancies_for_offer,
            "/respond_to_offer": self.respond_to_offer,
            "/get_wallet_summaries": self.get_wallet_summaries,
            "/get_public_keys": self.get_public_keys,
            "/generate_mnemonic": self.generate_mnemonic,
            "/log_in": self.log_in,
            "/add_key": self.add_key,
            "/delete_key": self.delete_key,
            "/delete_all_keys": self.delete_all_keys,
            "/get_private_key": self.get_private_key,
            "/get_trade": self.get_trade,
            "/get_all_trades": self.get_all_trades,
            "/cancel_trade": self.cancel_trade,
            "/create_backup": self.create_backup,
            "/rl_set_user_info": self.rl_set_user_info,
            "/send_clawback_transaction:": self.send_clawback_transaction,
        }

    async def rl_set_user_info(self, request):
        wallet_id = uint32(int(request["wallet_id"]))
        rl_user = self.service.wallet_state_manager.wallets[wallet_id]
        origin = request["origin"]
        try:
            success = await rl_user.set_user_info(
                uint64(request["interval"]),
                uint64(request["limit"]),
                origin["parent_coin_info"],
                origin["puzzle_hash"],
                origin["amount"],
                request["admin_pubkey"],
            )
            return {"success": success}
        except Exception as e:
            data = {
                "success": False,
                "reason": str(e),
            }
            return data

    async def get_trade(self, request: Dict):
        if self.service is None:
            return {"success": False}
        if self.service.wallet_state_manager is None:
            return {"success": False}

        trade_mgr = self.service.wallet_state_manager.trade_manager

        trade_id = request["trade_id"]
        trade: Optional[TradeRecord] = await trade_mgr.get_trade_by_id(trade_id)
        if trade is None:
            response = {
                "success": False,
                "error": f"No trade with trade id: {trade_id}",
            }
            return response

        result = trade_record_to_dict(trade)
        response = {"success": True, "trade": result}
        return response

    async def get_all_trades(self, request: Dict):
        if self.service is None:
            return {"success": False}
        if self.service.wallet_state_manager is None:
            return {"success": False}

        trade_mgr = self.service.wallet_state_manager.trade_manager

        all_trades = await trade_mgr.get_all_trades()
        result = []
        for trade in all_trades:
            result.append(trade_record_to_dict(trade))

        response = {"success": True, "trades": result}
        return response

    async def cancel_trade(self, request: Dict):
        if self.service is None:
            return {"success": False}
        if self.service.wallet_state_manager is None:
            return {"success": False}

        wsm = self.service.wallet_state_manager
        secure = request["secure"]
        trade_id = hexstr_to_bytes(request["trade_id"])

        if secure:
            await wsm.trade_manager.cancel_pending_offer_safely(trade_id)
        else:
            await wsm.trade_manager.cancel_pending_offer(trade_id)

        response = {"success": True}
        return response

    async def _state_changed(self, *args) -> List[str]:
        if len(args) < 2:
            return []

        change = args[0]
        wallet_id = args[1]
        data = {
            "state": change,
        }
        if wallet_id is not None:
            data["wallet_id"] = wallet_id
        return [create_payload("state_changed", data, "chia_wallet", "wallet_ui")]

    async def get_next_puzzle_hash(self, request: Dict) -> Dict:
        """
        Returns a new puzzlehash
        """
        if self.service is None:
            return {"success": False}

        wallet_id = uint32(int(request["wallet_id"]))
        if self.service.wallet_state_manager is None:
            return {"success": False}
        wallet = self.service.wallet_state_manager.wallets[wallet_id]

        if wallet.wallet_info.type == WalletType.STANDARD_WALLET.value:
            raw_puzzle_hash = await wallet.get_new_puzzlehash()
            puzzle_hash = encode_puzzle_hash(raw_puzzle_hash)
        elif wallet.wallet_info.type == WalletType.COLOURED_COIN.value:
            raw_puzzle_hash = await wallet.get_new_inner_hash()
            puzzle_hash = encode_puzzle_hash(raw_puzzle_hash)
        else:
            return {
                "success": False,
                "reason": "Wallet type cannnot create puzzle hashes",
            }

        response = {
            "success": True,
            "wallet_id": wallet_id,
            "puzzle_hash": puzzle_hash,
        }

        return response

    async def send_transaction(self, request):
        log.debug("rpc_api request: " + str(request))
        wallet_id = int(request["wallet_id"])
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        try:
            tx = await wallet.generate_signed_transaction_dict(request)
        except Exception as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to generate signed transaction. Error: {e}",
                "id": wallet_id,
            }
            return data
        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
                "id": wallet_id,
            }
            return data
        try:
            await wallet.push_transaction(tx)
        except Exception as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to push transaction {e}",
                "id": wallet_id,
            }
            return data
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.service.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS", "id": wallet_id}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err, "id": wallet_id}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err, "id": wallet_id}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
                "id": wallet_id,
            }

        return data

    async def get_transactions(self, request):
        wallet_id = int(request["wallet_id"])
        transactions = await self.service.wallet_state_manager.get_all_transactions(
            wallet_id
        )
        formatted_transactions = []

        for tx in transactions:
            formatted = tx.to_json_dict()
            formatted["to_puzzle_hash"] = encode_puzzle_hash(tx.to_puzzle_hash)
            formatted_transactions.append(formatted)

        response = {
            "success": True,
            "txs": formatted_transactions,
            "wallet_id": wallet_id,
        }
        return response

    async def farm_block(self, request):
        puzzle_hash = request["puzzle_hash"]
        raw_puzzle_hash = decode_puzzle_hash(puzzle_hash)
        request = FarmNewBlockProtocol(raw_puzzle_hash)
        msg = OutboundMessage(
            NodeType.FULL_NODE,
            Message("farm_new_block", request),
            Delivery.BROADCAST,
        )

        self.service.server.push_message(msg)
        return {"success": True}

    async def get_wallet_balance(self, request: Dict):
        if self.service.wallet_state_manager is None:
            return {"success": False}
        wallet_id = uint32(int(request["wallet_id"]))
        wallet = self.service.wallet_state_manager.wallets[wallet_id]
        balance = await wallet.get_confirmed_balance()
        pending_balance = await wallet.get_unconfirmed_balance()
        spendable_balance = await wallet.get_spendable_balance()
        pending_change = await wallet.get_pending_change_balance()
        if wallet.wallet_info.type == WalletType.COLOURED_COIN.value:
            frozen_balance = 0
        else:
            frozen_balance = await wallet.get_frozen_amount()

        wallet_balance = {
            "wallet_id": wallet_id,
            "confirmed_wallet_balance": balance,
            "unconfirmed_wallet_balance": pending_balance,
            "spendable_balance": spendable_balance,
            "frozen_balance": frozen_balance,
            "pending_change": pending_change,
        }

        return {"success": True, "wallet_balance": wallet_balance}

    async def get_sync_status(self, request: Dict):
        if self.service.wallet_state_manager is None:
            return {"success": False}
        syncing = self.service.wallet_state_manager.sync_mode

        return {"success": True, "syncing": syncing}

    async def get_height_info(self, request: Dict):
        if self.service.wallet_state_manager is None:
            return {"success": False}
        lca = self.service.wallet_state_manager.lca
        height = self.service.wallet_state_manager.block_records[lca].height

        response = {"success": True, "height": height}

        return response

    async def create_new_wallet(self, request):
        config, wallet_state_manager, main_wallet = self.get_wallet_config()

        if request["wallet_type"] == "cc_wallet":
            if request["mode"] == "new":
                try:
                    cc_wallet: CCWallet = await CCWallet.create_new_cc(
                        wallet_state_manager, main_wallet, request["amount"]
                    )
                    colour = cc_wallet.get_colour()
                    return {
                        "success": True,
                        "type": cc_wallet.wallet_info.type,
                        "colour": colour,
                        "wallet_id": cc_wallet.wallet_info.id,
                    }
                except Exception as e:
                    log.error("FAILED {e}")
                    return {"success": False, "reason": str(e)}
            elif request["mode"] == "existing":
                try:
                    cc_wallet = await CCWallet.create_wallet_for_cc(
                        wallet_state_manager, main_wallet, request["colour"]
                    )
                    return {"success": True, "type": cc_wallet.wallet_info.type}
                except Exception as e:
                    log.error("FAILED2 {e}")
                    return {"success": False, "reason": str(e)}
        if request["wallet_type"] == "rl_wallet":
            if request["rl_type"] == "admin":
                log.info("Create rl admin wallet")
                try:
                    rl_admin: RLWallet = await RLWallet.create_rl_admin(
                        wallet_state_manager
                    )
                    success = await rl_admin.admin_create_coin(
                        uint64(int(request["interval"])),
                        uint64(int(request["limit"])),
                        request["pubkey"],
                        uint64(int(request["amount"])),
                    )
                    return {
                        "success": success,
                        "id": rl_admin.wallet_info.id,
                        "type": rl_admin.wallet_info.type,
                        "origin": rl_admin.rl_info.rl_origin,
                        "pubkey": rl_admin.rl_info.admin_pubkey.hex(),
                    }
                except Exception as e:
                    log.error("FAILED {e}")
                    return {"success": False, "reason": str(e)}
            elif request["rl_type"] == "user":
                log.info("Create rl user wallet")
                try:
                    rl_user: RLWallet = await RLWallet.create_rl_user(
                        wallet_state_manager
                    )

                    return {
                        "success": True,
                        "id": rl_user.wallet_info.id,
                        "type": rl_user.wallet_info.type,
                        "pubkey": rl_user.rl_info.user_pubkey.hex(),
                    }
                except Exception as e:
                    log.error("FAILED {e}")
                    return {"success": False, "reason": str(e)}
        if request["wallet_type"] == "did_wallet":
            did_wallet = await DIDWallet.create_new_did_wallet(
                wallet_state_manager,
                main_wallet,
                request["amount"],
                request["backup_ids"],
            )
            return {"success": True, "type": did_wallet.wallet_info.type.name}

    def get_wallet_config(self):
        return (
            self.service.config,
            self.service.wallet_state_manager,
            self.service.wallet_state_manager.main_wallet,
        )

    async def get_wallets(self, request: Dict):
        if self.service.wallet_state_manager is None:
            return {"success": False}
        wallets: List[
            WalletInfo
        ] = await self.service.wallet_state_manager.get_all_wallets()

        response = {"wallets": wallets, "success": True}

        return response

    async def rl_set_admin_info(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.service.wallet_state_manager.wallets[wallet_id]
        user_pubkey = request["user_pubkey"]
        limit = uint64(int(request["limit"]))
        interval = uint64(int(request["interval"]))
        amount = uint64(int(request["amount"]))

        success = await wallet.admin_create_coin(interval, limit, user_pubkey, amount)

        response = {"success": success}

        return response

    async def did_update_recovery_ids(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]
        recovery_list = request["new_list"]
        await wallet.update_recovery_list(recovery_list)
        # Update coin with new ID info
        updated_puz = await wallet.get_new_puzzle()
        spend_bundle = await wallet.create_spend(updated_puz.get_tree_hash())
        if spend_bundle is not None:
            return {"success": True}
        return {"success": False}

    async def did_spend(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]
        spend_bundle = await wallet.create_spend(request["puzzlehash"])
        if spend_bundle is not None:
            return {"success": True}
        return {"success": False}

    async def did_get_id(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]
        id = wallet.get_my_ID()
        try:
            coins = wallet.select_coins(1)
            coin = coins.pop()
            return {"success": True, "id": id, "coin_id": coin.name()}
        except Exception:
            return {"success": False}

    async def did_get_recovery_list(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]
        recovery_list = wallet.did_info.backup_ids
        return {"success": True, "recover_list": recovery_list}

    async def did_recovery_spend(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]

        spend_bundle_list = []
        for i in request["spend_bundles"]:
            spend_bundle_list.append(SpendBundle.from_bytes(bytes.fromhex(i)))
        # info_dict {0xidentity: "(0xparent_info 0xinnerpuz amount)"}
        info_dict = request["info_dict"]
        my_recovery_list: List[bytes] = wallet.did_info.backup_ids
        if len(info_dict) != len(my_recovery_list):
            return False
        # convert info dict into recovery list - same order as wallet
        info_list = []
        for entry in my_recovery_list:
            info_list.append(info_dict[entry.hex())
        message_spend_bundle = SpendBundle.aggregate(spend_bundle_list)

        success = await wallet.recovery_spend(
            request["coin_name"], request["puzhash"], info_list, message_spend_bundle
        )
        return {"success": success}

    async def did_create_attest(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: DIDWallet = self.service.wallet_state_manager.wallets[wallet_id]
        info = await wallet.get_info_for_recovery()
        spend_bundle = await wallet.create_attestment(
            request["coin_name"], request["puzhash"]
        )
        return {"success": True, "message_spend_bundle": bytes(spend_bundle).hex(), "info": info}

    async def cc_set_name(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.service.wallet_state_manager.wallets[wallet_id]
        await wallet.set_name(str(request["name"]))
        response = {"wallet_id": wallet_id, "success": True}
        return response

    async def cc_get_name(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.service.wallet_state_manager.wallets[wallet_id]
        name: str = await wallet.get_name()
        response = {"wallet_id": wallet_id, "name": name}
        return response

    async def cc_spend(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.service.wallet_state_manager.wallets[wallet_id]
        encoded_puzzle_hash = request["innerpuzhash"]
        puzzle_hash = decode_puzzle_hash(encoded_puzzle_hash)

        try:
            tx = await wallet.generate_signed_transaction(
                request["amount"], puzzle_hash
            )
        except Exception as e:
            data = {"status": "FAILED", "reason": f"{e}", "id": wallet_id}
            return data

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
                "id": wallet_id,
            }
            return data
        try:
            await wallet.wallet_state_manager.add_pending_transaction(tx)
        except Exception as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to push transaction {e}",
            }
            return data

        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.service.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS", "id": wallet_id}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err, "id": wallet_id}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err, "id": wallet_id}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
                "id": wallet_id,
            }

        return data

    async def cc_get_colour(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.service.wallet_state_manager.wallets[wallet_id]
        colour: str = wallet.get_colour()
        response = {"colour": colour, "wallet_id": wallet_id}
        return response

    async def get_wallet_summaries(self, request: Dict):
        if self.service.wallet_state_manager is None:
            return {"success": False}
        wallet_summaries = {}
        for wallet_id in self.service.wallet_state_manager.wallets:
            wallet = self.service.wallet_state_manager.wallets[wallet_id]
            balance = await wallet.get_confirmed_balance()
            type = wallet.wallet_info.type
            if type == WalletType.COLOURED_COIN.value:
                name = wallet.cc_info.my_colour_name
                colour = wallet.get_colour()
                wallet_summaries[wallet_id] = {
                    "type": type,
                    "balance": balance,
                    "name": name,
                    "colour": colour,
                }
            else:
                wallet_summaries[wallet_id] = {"type": type, "balance": balance}
        return {"success": True, "wallet_summaries": wallet_summaries}

    async def get_discrepancies_for_offer(self, request):
        file_name = request["filename"]
        file_path = Path(file_name)
        (
            success,
            discrepancies,
            error,
        ) = await self.service.wallet_state_manager.trade_manager.get_discrepancies_for_offer(
            file_path
        )

        if success:
            response = {"success": True, "discrepancies": discrepancies}
        else:
            response = {"success": False, "error": error}

        return response

    async def create_offer_for_ids(self, request):
        offer = request["ids"]
        file_name = request["filename"]
        (
            success,
            spend_bundle,
            error,
        ) = await self.service.wallet_state_manager.trade_manager.create_offer_for_ids(
            offer, file_name
        )
        if success:
            self.service.wallet_state_manager.trade_manager.write_offer_to_disk(
                Path(file_name), spend_bundle
            )
            response = {"success": success}
        else:
            response = {"success": success, "reason": error}

        return response

    async def create_backup(self, request):
        file_path = Path(request["file_path"])
        await self.service.wallet_state_manager.create_wallet_backup(file_path)
        response = {"success": True}
        return response

    async def get_backup_info(self, request: Dict):
        file_path = Path(request["file_path"])
        sk = None
        if "words" in request:
            mnemonic = request["words"]
            passphrase = ""
            try:
                sk = self.service.keychain.add_private_key(
                    " ".join(mnemonic), passphrase
                )
            except KeyError as e:
                return {
                    "success": False,
                    "error": f"The word '{e.args[0]}' is incorrect.'",
                    "word": e.args[0],
                }
            except ValueError as e:
                return {
                    "success": False,
                    "error": e.args[0],
                }
        elif "fingerprint" in request:
            sk, seed = await self._get_private_key(request["fingerprint"])

        if sk is None:
            return {
                "success": False,
                "error": "Unable to decrypt the backup file.",
            }
        backup_info = get_backup_info(file_path, sk)
        response = {"success": True, "backup_info": backup_info}
        return response

    async def respond_to_offer(self, request):
        file_path = Path(request["filename"])
        (
            success,
            trade_record,
            reason,
        ) = await self.service.wallet_state_manager.trade_manager.respond_to_offer(
            file_path
        )
        if success:
            response = {"success": success}
        else:
            response = {"success": success, "reason": reason}
        return response

    async def get_public_keys(self, request: Dict):
        fingerprints = [
            (sk.get_g1().get_fingerprint(), seed is not None)
            for (sk, seed) in self.service.keychain.get_all_private_keys()
        ]
        response = {"success": True, "public_key_fingerprints": fingerprints}
        return response

    async def _get_private_key(
        self, fingerprint
    ) -> Tuple[Optional[PrivateKey], Optional[bytes]]:
        for sk, seed in self.service.keychain.get_all_private_keys():
            if sk.get_g1().get_fingerprint() == fingerprint:
                return sk, seed
        return None, None

    async def get_private_key(self, request):
        fingerprint = request["fingerprint"]
        sk, seed = await self._get_private_key(fingerprint)
        if sk is not None:
            s = bytes_to_mnemonic(seed) if seed is not None else None
            return {
                "success": True,
                "private_key": {
                    "fingerprint": fingerprint,
                    "sk": bytes(sk).hex(),
                    "pk": bytes(sk.get_g1()).hex(),
                    "seed": s,
                },
            }
        return {"success": False, "private_key": {"fingerprint": fingerprint}}

    async def log_in(self, request):
        await self.stop_wallet()
        fingerprint = request["fingerprint"]
        type = request["type"]

        if type == "skip":
            started = await self.service._start(
                fingerprint=fingerprint, skip_backup_import=True
            )
        elif type == "restore_backup":
            file_path = Path(request["file_path"])
            started = await self.service._start(
                fingerprint=fingerprint, backup_file=file_path
            )
        else:
            started = await self.service._start(fingerprint)

        if started is True:
            return {"success": True}
        else:
            if self.service.backup_initialized is False:
                return {"success": False, "error": "not_initialized"}

        return {"success": False, "error": "Unknown Error"}

    async def add_key(self, request):
        if "mnemonic" in request:
            # Adding a key from 24 word mnemonic
            mnemonic = request["mnemonic"]
            passphrase = ""
            try:
                sk = self.service.keychain.add_private_key(
                    " ".join(mnemonic), passphrase
                )
            except KeyError as e:
                return {
                    "success": False,
                    "error": f"The word '{e.args[0]}' is incorrect.'",
                    "word": e.args[0],
                }
            except ValueError as e:
                return {
                    "success": False,
                    "error": e.args[0],
                }

        else:
            return {"success": False}

        fingerprint = sk.get_g1().get_fingerprint()
        await self.stop_wallet()

        # Makes sure the new key is added to config properly
        started = False
        check_keys(self.service.root_path)
        type = request["type"]
        if type == "new_wallet":
            started = await self.service._start(
                fingerprint=fingerprint, new_wallet=True
            )
        elif type == "skip":
            started = await self.service._start(
                fingerprint=fingerprint, skip_backup_import=True
            )
        elif type == "restore_backup":
            file_path = Path(request["file_path"])
            started = await self.service._start(
                fingerprint=fingerprint, backup_file=file_path
            )

        if started is True:
            return {"success": True}
        else:
            return {"success": False}

    async def delete_key(self, request):
        await self.stop_wallet()
        fingerprint = request["fingerprint"]
        self.service.keychain.delete_key_by_fingerprint(fingerprint)
        path = path_from_root(
            self.service.root_path,
            f"{self.service.config['database_path']}-{fingerprint}",
        )
        if path.exists():
            path.unlink()
        return {"success": True}

    async def clean_all_state(self):
        self.service.keychain.delete_all_keys()
        path = path_from_root(
            self.service.root_path, self.service.config["database_path"]
        )
        if path.exists():
            path.unlink()

    async def stop_wallet(self):
        if self.service is not None:
            self.service._close()
            await self.service._await_closed()

    async def delete_all_keys(self, request: Dict):
        await self.stop_wallet()
        await self.clean_all_state()
        response = {"success": True}
        return response

    async def generate_mnemonic(self, request: Dict):
        mnemonic = generate_mnemonic()
        response = {"success": True, "mnemonic": mnemonic}
        return response

    async def send_clawback_transaction(self, request):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.service.wallet_state_manager.wallets[wallet_id]
        try:
            tx = await wallet.clawback_rl_coin_transaction()
        except Exception as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to generate signed transaction {e}",
            }
            return data
        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return data
        try:
            await wallet.push_transaction(tx)
        except Exception as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to push transaction {e}",
            }
            return data
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.service.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }

        return data
