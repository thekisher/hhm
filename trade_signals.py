import random
from typing import Optional

from dydx_v4_client.network import make_mainnet
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient
from dydx_v4_client.market import Market
from dydx_v4_client.wallet import Wallet
from dydx_v4_client.types import Order, OrderFlags, OrderType


class DydxOrderClient:
    def __init__(
        self,
        mnemonic: str= "still endorse use choose monkey equal jungle ketchup obscure put stumble eye minimum ritual follow neck rally coin funny sock broccoli bracket kite render",
        address: str="dydx1kydnehq9hfqqrt28jc2nhaxux33hk2sh9zw84q",
        clob_pair_id: int,
        node_url: str = "grpc://oegs.dydx.trade:443",
        rest_indexer_url: str = "https://indexer.dydx.trade/v4",
        websocket_indexer_url: str = "wss://indexer.dydx.trade/v4/ws",
    ):
        """
        Simple wrapper for placing limit orders on a single CLOB pair.
        """
        self.mnemonic = mnemonic
        self.address = address
        self.clob_pair_id = clob_pair_id

        network = make_mainnet(
            node_url=node_url,
            rest_indexer=rest_indexer_url,
            websocket_indexer=websocket_indexer_url,
        ).node  # [[Available clients](https://docs.dydx.xyz/interaction/endpoints#available-clients)]

        self._network = network
        self._node: Optional[NodeClient] = None
        self._indexer: Optional[IndexerClient] = None
        self._wallet: Optional[Wallet] = None
        self._market: Optional[Market] = None

    async def connect(self):
        """
        Initialize Node client, Indexer client, Wallet and Market helper.
        Call this once before placing orders.
        """
        # Node (trading / private) client
        self._node = await NodeClient.connect(self._network)  # [[Private Node API](https://docs.dydx.xyz/node-client/private#private-node-api)]

        # Indexer (HTTP / market data) client
        self._indexer = IndexerClient(self._network.rest_indexer)

        # Fetch market params for this CLOB pair
        markets_resp = await self._indexer.markets.get_perpetual_markets(self.clob_pair_id)
        self._market = Market(markets_resp["markets"][self.clob_pair_id])  # [[Place an order](https://docs.dydx.xyz/interaction/trading#place-an-order)]

        # Wallet for signing
        self._wallet = await Wallet.from_mnemonic(self._node, self.mnemonic, self.address)  # [[Wallet setup](https://docs.dydx.xyz/interaction/wallet-setup#wallet-setup)]

    async def place_limit_order(
        self,
        side: Order.Side,
        size: float,
        price: float,
        post_only: bool = False,
        reduce_only: bool = False,
        good_til_blocks_ahead: int = 10,
    ) -> str:
        """
        Build and place a short-term LIMIT order on the configured market.
        Returns tx hash.
        """
        assert self._node and self._wallet and self._market

        # Build structured order_id (short-term)
        order_id = self._market.order_id(
            self.address,
            0,  # subaccount index
            random.randint(0, 1_000_000_000),  # client_id
            OrderFlags.SHORT_TERM,  # short-term order [[Place an order](https://docs.dydx.xyz/interaction/trading#identifying-the-order)]
        )

        # Validity window
        good_til_block = await self._node.latest_block_height() + good_til_blocks_ahead  # [[Place an order](https://docs.dydx.xyz/interaction/trading#place-an-order)]

        # Time in force
        tif = (
            Order.TimeInForce.POST_ONLY
            if post_only
            else Order.TimeInForce.TIME_IN_FORCE_UNSPECIFIED
        )  # [[Time in Force](https://docs.dydx.xyz/types/time_in_force#time-in-force)]

        # Build Order
        order = self._market.order(
            order_id=order_id,
            order_type=OrderType.LIMIT,
            side=side,
            size=size,
            price=price,
            time_in_force=tif,
            reduce_only=reduce_only,
            good_til_block=good_til_block,
        )  # [[Order](https://docs.dydx.xyz/types/order#order)]

        # Send via Node client
        tx_hash = await self._node.place_order(self._wallet, order)  # [[Private Node API](https://docs.dydx.xyz/node-client/private#private-node-api)]
        return tx_hash
