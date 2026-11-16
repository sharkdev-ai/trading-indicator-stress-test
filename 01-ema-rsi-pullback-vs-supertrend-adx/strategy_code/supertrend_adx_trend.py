from datetime import timedelta, datetime
from dataclasses import dataclass, field
from sharkdev_imports import *

@dataclass
class TimeframeData:

    multiplier: int
    resolution: Resolution

@dataclass
class IndicatorData:

    name: str
    timeframe: TimeframeData
    indicator_class: Indicator

@dataclass
class SymbolData:

    symbol: Symbol
    indicators: dict[str, Indicator] = field(default_factory=dict)
    end_time: datetime = None
    ask_open: float = None
    ask_high: float = None
    ask_low: float = None
    ask_close: float = None
    last_ask_size: float = None
    bid_open: float = None
    bid_high: float = None
    bid_low: float = None
    bid_close: float = None
    last_bid_size: float = None
    mid_open: float = None
    mid_high: float = None
    mid_low: float = None
    mid_close: float = None
    spread: float = None

    entry_ticket: OrderTicket = None
    stop_ticket: OrderTicket = None
    target_ticket: OrderTicket = None
    trailing_ticket: OrderTicket = None

    pending_entry_time: datetime = None
    position_open_time: datetime = None
    last_exit_time: datetime = None

class Strategy(QCAlgorithm):

    def initialize(self):
        self.settings.automatic_indicator_warm_up = True
        self.set_time_zone(TimeZones.NEW_YORK)
        self.set_brokerage_model(BrokerageName.BINANCE, AccountType.CASH)

        self.security_type: str = "crypto"

        if not self.live_mode:
            self.set_cash(10000.0)
            self.set_start_date(2024, 1, 1)
            self.set_end_date(2025, 12, 31)

        self._configure_warm_up()

        self.data = {}
        self.order_to_data = {}
        self.primary_symbol = None
        self.pos_sizes = {}

        self._initialize_strategy()
        self._recover_position_state()

    def _recover_position_state(self):
        for data in self.data.values():
            recover_position_state(self, data)

    def _configure_warm_up(self):
        days = resolve_warmup_days(
            2, 30, self.live_mode
        )
        self.set_warm_up(timedelta(days=days), Resolution.HOUR)

    def _map_live_ticker(self, ticker):
        if not self.live_mode or self.security_type != "crypto":
            return ticker
        return derive_live_ticker(ticker)

    def _is_effectively_flat(self, data, holdings):
        return is_effectively_flat(self, data.symbol, holdings)

    def _is_effectively_invested(self, data, holdings):
        return is_effectively_invested(self, data.symbol, holdings)

    def on_brokerage_message(self, message):
        self.log(f"BROKERAGE MESSAGE: {message}")
        self._recover_position_state()

    def on_brokerage_disconnect(self):
        self.log("BROKERAGE DISCONNECT")

    def on_brokerage_reconnect(self):
        self.log("BROKERAGE RECONNECT")
        self._recover_position_state()

    def on_warmup_finished(self):
        try:
            entries = []
            for ticker, data in self.data.items():
                for name, indicator in data.indicators.items():
                    entries.append(
                        (ticker, name, indicator.is_ready, indicator.current.value)
                    )
            ready_count = sum(1 for _, _, ready, _ in entries if ready)
            self.log(
                f"WARMUP FINISHED: live_mode={self.live_mode} "
                f"is_warming_up={self.is_warming_up} "
                f"indicators_ready={ready_count}/{len(entries)}"
            )
            if not entries:
                self.log(
                    "WARMUP INDICATOR: none registered by generated code "
                    "(expected only when the spec declares no indicators)"
                )
            for ticker, name, ready, value in entries:
                self.log(
                    f"WARMUP INDICATOR: {ticker}:{name} ready={ready} value={value}"
                )
        except Exception as exception:
            self.log(f"WARMUP FINISHED: readiness log failed: {exception}")

    def _resolve_consolidator_timedelta(self, timeframe: TimeframeData) -> timedelta:
        if timeframe.resolution == Resolution.DAILY:
            return timedelta(days=timeframe.multiplier)
        return timedelta(hours=timeframe.multiplier)

    def _add_symbol_consolidator(self, symbol: Symbol, symbol_timedelta: timedelta):
        consolidator = QuoteBarConsolidator(symbol_timedelta)
        consolidator.data_consolidated += self._on_consolidated_bar
        self.subscription_manager.add_consolidator(symbol, consolidator, TickType.QUOTE)

    def _add_indicator_consolidator(
        self, symbol: Symbol, indicator: Indicator, indicator_timedelta: timedelta
    ):
        consolidator = TradeBarConsolidator(indicator_timedelta)
        self.register_indicator(
            symbol,
            indicator,
            consolidator,
        )

    def _add_symbol_subscription(
        self,
        ticker: str,
        symbol_timeframe: TimeframeData,
        indicators: list[IndicatorData],
    ):
        resolution = symbol_timeframe.resolution
        market = Market.BINANCE if self.live_mode else Market.DUKASCOPY
        if self.live_mode:
            ticker = self._map_live_ticker(ticker)

        if self.security_type == "crypto":
            security = self.add_crypto(
                ticker,
                resolution,
                fill_forward=False,
                market=market,
            )
        elif self.security_type == "forex":
            security = self.add_forex(
                ticker,
                resolution,
                fill_forward=False,
                market=market,
                leverage=50.0,
            )
            security.set_fee_model(InteractiveBrokersFeeModel())
        elif self.security_type == "equity":
            security = self.add_equity(
                ticker,
                resolution,
                fill_forward=False,
                market=market,
            )
            security.is_tradable = False
        else:
            raise ValueError(
                "Only support forex, crypto and equity, other symbol types will be added soon!"
            )

        data = SymbolData(security.symbol)

        for indicator in indicators:
            time_delta = self._resolve_consolidator_timedelta(indicator.timeframe)
            self._add_indicator_consolidator(
                data.symbol, indicator.indicator_class, time_delta
            )
            data.indicators[indicator.name] = indicator.indicator_class

        symbol_timedelta = self._resolve_consolidator_timedelta(symbol_timeframe)
        self._add_symbol_consolidator(data.symbol, symbol_timedelta)

        self.data[ticker] = data

        if self.primary_symbol is None:
            self.primary_symbol = data.symbol
            self.set_benchmark(data.symbol)

        return data

    def _initialize_strategy(self):

        self.stop_loss_pct = float(self.get_parameter("stop_loss_pct", 0.03))
        self.take_profit_pct = float(self.get_parameter("take_profit_pct", 0.06))
        self.timeout_bars = int(self.get_parameter("timeout_bars", 48))
        self.adx_threshold = int(self.get_parameter("adx_threshold", 25))
        self.supertrend_atr_period = int(self.get_parameter("supertrend_atr_period", 10))
        self.supertrend_multiplier = float(self.get_parameter("supertrend_multiplier", 3.0))

        tf = TimeframeData(multiplier=2, resolution=Resolution.HOUR)
        indicators = [
            IndicatorData(
                name="atr",
                timeframe=tf,
                indicator_class=AverageTrueRange(self.supertrend_atr_period)
            ),
            IndicatorData(
                name="adx",
                timeframe=tf,
                indicator_class=AverageDirectionalIndex(14)
            )
        ]

        data = self._add_symbol_subscription("BTCUSD", tf, indicators)
        self.pos_sizes[data.symbol.value] = 0.9

    def _is_quote_valid(self, bar: QuoteBar) -> bool:
        if bar is None:
            return False
        if bar.ask is None or bar.bid is None:
            return False

        def _is_valid_number(value):
            if value is None:
                return False
            try:
                return value == value
            except Exception:
                return False

        for side_bar in [bar.ask, bar.bid]:
            for field in ["open", "high", "low", "close"]:
                if not _is_valid_number(getattr(side_bar, field, None)):
                    return False

        return True

    def _is_indicators_valid(self, data: SymbolData) -> bool:
        for indicator in data.indicators.values():
            if not indicator.is_ready:
                return False
            if (
                indicator.current.value is None
                or indicator.current.value != indicator.current.value
            ):
                return False
        return True

    def _update_quote(self, data: SymbolData, bar: QuoteBar):

        data.end_time = bar.end_time

        data.ask_open = bar.ask.open
        data.ask_high = bar.ask.high
        data.ask_low = bar.ask.low
        data.ask_close = bar.ask.close
        data.last_ask_size = bar.last_ask_size

        data.bid_open = bar.bid.open
        data.bid_high = bar.bid.high
        data.bid_low = bar.bid.low
        data.bid_close = bar.bid.close
        data.last_bid_size = bar.last_bid_size

        data.mid_open = bar.open
        data.mid_high = bar.high
        data.mid_low = bar.low
        data.mid_close = bar.close
        data.spread = data.ask_close - data.bid_close

    def _update_indicators(self, data: SymbolData):

        atr = data.indicators["atr"].current.value
        adx = data.indicators["adx"].current.value

        if getattr(data, "curr_adx", None) is not None:
            data.prev_adx = data.curr_adx
        else:
            data.prev_adx = adx
        data.curr_adx = adx

        hl2 = (data.mid_high + data.mid_low) / 2.0
        basic_upper = hl2 + (self.supertrend_multiplier * atr)
        basic_lower = hl2 - (self.supertrend_multiplier * atr)

        prev_final_upper = getattr(data, "curr_final_upper", basic_upper)
        prev_final_lower = getattr(data, "curr_final_lower", basic_lower)
        prev_trend = getattr(data, "curr_trend", 1)
        prev_close = getattr(data, "curr_close", data.mid_close)

        if basic_upper < prev_final_upper or prev_close > prev_final_upper:
            curr_final_upper = basic_upper
        else:
            curr_final_upper = prev_final_upper

        if basic_lower > prev_final_lower or prev_close < prev_final_lower:
            curr_final_lower = basic_lower
        else:
            curr_final_lower = prev_final_lower

        if prev_trend == 1:
            curr_trend = -1 if data.mid_close < curr_final_lower else 1
        else:
            curr_trend = 1 if data.mid_close > curr_final_upper else -1

        data.prev_trend = prev_trend
        data.curr_trend = curr_trend
        data.prev_final_upper = prev_final_upper
        data.curr_final_upper = curr_final_upper
        data.prev_final_lower = prev_final_lower
        data.curr_final_lower = curr_final_lower
        data.curr_close = data.mid_close

    def _on_consolidated_bar(self, sender, bar: QuoteBar):
        data = self.data.get(bar.symbol.value, None)

        if data is None or not self._is_quote_valid(bar):
            return

        self._update_quote(data, bar)

        if not self._is_indicators_valid(data):
            return

        self._update_indicators(data)

        if self.is_warming_up:
            return

        self._process_symbol(data)

    def _to_lot_size_multiple(self, symbol: Symbol, raw_quantity: float) -> float:
        return to_lot_size_multiple(self, symbol, raw_quantity)

    def _to_tick_size_multiple(self, symbol: Symbol, price: float) -> float:
        return to_tick_size_multiple(self, symbol, price)

    def _cancel_bracket_orders(self, data: SymbolData):

        self._cancel_ticket(data.stop_ticket, "Cancel stop")
        self._cancel_ticket(data.target_ticket, "Cancel target")
        self._cancel_ticket(data.trailing_ticket, "Cancel trailing")
        self._clear_ticket(data, "stop_ticket")
        self._clear_ticket(data, "target_ticket")
        self._clear_ticket(data, "trailing_ticket")

    def _is_open_order(self, ticket: OrderTicket):
        return ticket is not None and ticket.status not in [
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.INVALID,
        ]

    def _has_pending_entry(self, data: SymbolData):
        return self._is_open_order(data.entry_ticket)

    def _cancel_ticket(self, ticket, tag):
        if self._is_open_order(ticket):
            ticket.cancel(tag)

    def _register_ticket(self, data: SymbolData, key: str, ticket: OrderTicket):
        old_ticket: OrderTicket = getattr(data, key)
        if (
            old_ticket is not None
            and ticket is not None
            and old_ticket.order_id != ticket.order_id
        ):
            self.order_to_data.pop(old_ticket.order_id, None)

        setattr(data, key, ticket)
        if ticket is not None:
            self.order_to_data[ticket.order_id] = data

    def _clear_ticket(self, data: SymbolData, key: str):
        ticket: OrderTicket = getattr(data, key)
        if ticket is not None:
            self.order_to_data.pop(ticket.order_id, None)
        setattr(data, key, None)

    def _get_order(self, order_id: int):
        return self.transactions.get_order_by_id(order_id)

    def _close_position(self, data: SymbolData, reason: str):

        self._cancel_bracket_orders(data)
        self.liquidate(data.symbol, reason)
        data.position_open_time = None
        data.last_exit_time = self.time

    def _calculate_entry_quantity(
        self, data: SymbolData, entry_direction: int
    ) -> float:

        if entry_direction == 0:
            return 0

        ticker = data.symbol.value
        position_size_pct = float(self.pos_sizes[ticker])
        target_pct = entry_direction * position_size_pct

        return self.calculate_order_quantity(data.symbol, target_pct)

    def _submit_order(
        self,
        data: SymbolData,
        order_type: str,
        quantity: float,
        *,
        target_pct: float = None,
        limit_price: float = None,
        stop_price: float = None,
        tag: str = "",
    ):
        symbol = data.symbol

        if order_type == "set_holdings":
            if target_pct is None:
                raise ValueError("set_holdings entry requires target_pct")
            quantity = self.calculate_order_quantity(symbol, target_pct)
            quantity = self._to_lot_size_multiple(symbol, quantity)
            if quantity == 0:
                return None
            return self.market_order(symbol=symbol, quantity=quantity, tag=tag)

        if order_type == "market":
            quantity = self._to_lot_size_multiple(symbol, quantity)
            if quantity == 0:
                return None
            return self.market_order(symbol=symbol, quantity=quantity, tag=tag)

        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit entry requires limit_price")
            return self.limit_order(
                symbol=symbol,
                quantity=quantity,
                limit_price=limit_price,
                tag=tag,
            )

        if order_type == "stop_limit":
            if stop_price is None or limit_price is None:
                raise ValueError("stop_limit entry requires stop_price and limit_price")
            return self.stop_limit_order(
                symbol=symbol,
                quantity=quantity,
                stop_price=stop_price,
                limit_price=limit_price,
                tag=tag,
            )

        if order_type == "stop_market":
            if stop_price is None:
                raise ValueError("stop_market entry requires stop_price")
            return self.stop_market_order(
                symbol=symbol,
                quantity=quantity,
                stop_price=stop_price,
                tag=tag,
            )

    def _process_symbol(self, data: SymbolData):

        if data.ask_close is None or data.bid_close is None or data.mid_close is None:
            return

        holdings = self.portfolio[data.symbol].quantity
        is_flat = self._is_effectively_flat(data, holdings)
        is_invested = self._is_effectively_invested(data, holdings)

        if is_invested and (data.position_open_time is None or (data.stop_ticket is None and data.target_ticket is None)):
            if data.position_open_time is None:
                data.position_open_time = self.time
            avg_price = self.portfolio[data.symbol].average_price
            entry_ref = avg_price if avg_price > 0 else data.mid_close
            self._cancel_bracket_orders(data)
            self._place_exit_bracket(data, entry_ref, holdings)

        prev_trend = getattr(data, "prev_trend", None)
        curr_trend = getattr(data, "curr_trend", None)
        curr_adx = getattr(data, "curr_adx", None)

        if prev_trend is None or curr_trend is None or curr_adx is None:
            return

        long_signal = (prev_trend == -1 and curr_trend == 1 and curr_adx > self.adx_threshold)
        long_exit_signal = (prev_trend == 1 and curr_trend == -1)

        if is_invested and data.position_open_time is not None:
            bars_held = (self.time - data.position_open_time).total_seconds() / (2 * 3600)
            if bars_held >= self.timeout_bars:
                self._close_position(data, "Timeout Exit")
                return
            if long_exit_signal:
                self._close_position(data, "Supertrend Bearish Flip Exit")
                return

        if not is_flat:
            return

        if self._has_pending_entry(data):
            return

        if data.last_exit_time is not None and data.last_exit_time == self.time:
            return

        if long_signal:
            qty = self._calculate_entry_quantity(data, 1)
            if qty > 0:
                self._submit_order(data, "market", qty, tag="Entry")

    def _place_exit_bracket(
        self, data: SymbolData, fill_price: float, fill_quantity: float
    ):

        if fill_quantity <= 0:
            return

        sl_price = fill_price * (1.0 - self.stop_loss_pct)
        sl_price = self._to_tick_size_multiple(data.symbol, sl_price)
        sl_limit = sl_price * 0.995
        sl_limit = self._to_tick_size_multiple(data.symbol, sl_limit)

        tp_price = fill_price * (1.0 + self.take_profit_pct)
        tp_price = self._to_tick_size_multiple(data.symbol, tp_price)
        tp_limit = tp_price

        qty = -self.portfolio[data.symbol].quantity if self.portfolio[data.symbol].quantity > 0 else -fill_quantity
        qty = self._to_lot_size_multiple(data.symbol, qty)

        if qty != 0:
            stop_ticket = self._submit_protective_order(
                data,
                "stop_limit",
                qty,
                stop_price=sl_price,
                limit_price=sl_limit,
                tag="SL"
            )
            self._register_ticket(data, "stop_ticket", stop_ticket)

            target_ticket = self._submit_protective_order(
                data,
                "stop_limit",
                qty,
                stop_price=tp_price,
                limit_price=tp_limit,
                tag="TP"
            )
            self._register_ticket(data, "target_ticket", target_ticket)

    def _submit_protective_order(
        self,
        data: SymbolData,
        order_type: str,
        quantity: float,
        *,
        stop_price: float = None,
        limit_price: float = None,
        trailing_percent: float = None,
        tag: str = "",
    ):

        symbol = data.symbol

        if order_type == "stop_limit":
            if stop_price is None or limit_price is None:
                raise ValueError(
                    "stop_limit protective order requires stop_price and limit_price"
                )
            return self.stop_limit_order(
                symbol=symbol,
                quantity=quantity,
                stop_price=stop_price,
                limit_price=limit_price,
                tag=tag,
            )

        if order_type == "stop_market":
            if stop_price is None:
                raise ValueError("stop_market protective order requires stop_price")
            return self.stop_market_order(
                symbol=symbol,
                quantity=quantity,
                stop_price=stop_price,
                tag=tag,
            )

        if order_type == "limit":
            if limit_price is None:
                raise ValueError("limit protective order requires limit_price")
            return self.limit_order(
                symbol=symbol,
                quantity=quantity,
                limit_price=limit_price,
                tag=tag,
            )

        if order_type == "trailing_stop":
            if trailing_percent is None:
                raise ValueError(
                    "trailing_stop protective order requires trailing_percent"
                )
            return self.trailing_stop_order(
                symbol=symbol,
                quantity=quantity,
                trailing_amount=trailing_percent,
                trailing_as_percentage=True,
                tag=tag,
            )

    def on_order_event(self, order_event: OrderEvent):
        data = self.order_to_data.get(order_event.order_id)
        if data is None:
            data = self.data.get(order_event.symbol.value)
        if data is None:
            return

        order = self._get_order(order_event.order_id)

        self._handle_order_event(
            data,
            order_event,
            order,
        )

    def _handle_order_event(self, data: SymbolData, order_event: OrderEvent, order):

        if order is not None and order.tag == "Entry" and order_event.status == OrderStatus.SUBMITTED:
            ticket = self.transactions.get_order_ticket(order_event.order_id)
            self._register_ticket(data, "entry_ticket", ticket)

        if order_event.status == OrderStatus.FILLED:
            order_tag = order.tag if order is not None else ""
            is_entry = (order_tag == "Entry") or (
                data.entry_ticket is not None and order_event.order_id == data.entry_ticket.order_id
            )

            if is_entry and order_event.fill_quantity > 0:
                data.pending_entry_time = None
                data.position_open_time = self.time
                self._place_exit_bracket(data, order_event.fill_price, order_event.fill_quantity)
                self._clear_ticket(data, "entry_ticket")
                return

            is_stop = (data.stop_ticket is not None and order_event.order_id == data.stop_ticket.order_id) or (order_tag == "SL")
            is_target = (data.target_ticket is not None and order_event.order_id == data.target_ticket.order_id) or (order_tag == "TP")
            is_trailing = (data.trailing_ticket is not None and order_event.order_id == data.trailing_ticket.order_id) or (order_tag == "Trail")

            if is_stop or is_target or is_trailing:
                if is_stop:
                    self._cancel_ticket(data.target_ticket, "OCO Cancel TP")
                    self._cancel_ticket(data.trailing_ticket, "OCO Cancel Trail")
                elif is_target:
                    self._cancel_ticket(data.stop_ticket, "OCO Cancel SL")
                    self._cancel_ticket(data.trailing_ticket, "OCO Cancel Trail")
                elif is_trailing:
                    self._cancel_ticket(data.stop_ticket, "OCO Cancel SL")
                    self._cancel_ticket(data.target_ticket, "OCO Cancel TP")

                self._clear_ticket(data, "stop_ticket")
                self._clear_ticket(data, "target_ticket")
                self._clear_ticket(data, "trailing_ticket")

                holdings = self.portfolio[data.symbol].quantity
                if self._is_effectively_flat(data, holdings):
                    data.position_open_time = None
                    data.last_exit_time = self.time
                return

        if order_event.status in [OrderStatus.CANCELED, OrderStatus.INVALID]:
            if data.entry_ticket is not None and order_event.order_id == data.entry_ticket.order_id:
                self._clear_ticket(data, "entry_ticket")
                data.pending_entry_time = None
            elif data.stop_ticket is not None and order_event.order_id == data.stop_ticket.order_id:
                self._clear_ticket(data, "stop_ticket")
            elif data.target_ticket is not None and order_event.order_id == data.target_ticket.order_id:
                self._clear_ticket(data, "target_ticket")
            elif data.trailing_ticket is not None and order_event.order_id == data.trailing_ticket.order_id:
                self._clear_ticket(data, "trailing_ticket")
