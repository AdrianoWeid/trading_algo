import datetime as dt
import time
import logging
from optibook.synchronous_client import Exchange

# connect to the exchange
exchange = Exchange()
exchange.connect()
logging.getLogger("client").setLevel("ERROR")

#stocks
STOCK_A_ID = "PHILIPS_A"
STOCK_B_ID = "PHILIPS_B"

POSITION_LIMIT = 100        # maximale Positionen
BASE_VOLUME = 10            # Handelsvolumen, minimum
EDGE_BASE = 0.01            # Mindestens nötiges Edge
SMOOTHING = 0.9             # Glättung von conv
SLEEP_SECONDS = 0.085         # Loop-Frequenz in Sekunden (max ~25 Orders/s), we check on every tick

conv = None

#sicherstellen, dass wir das trade limit nicht überschreiten
def trade_would_breach_position_limit(instrument_id, volume, side, positions, position_limit=POSITION_LIMIT):
    position_instrument = positions[instrument_id]

    if side == "bid":
        return position_instrument + volume > position_limit
    elif side == "ask":
        return position_instrument - volume < -position_limit
    # error when side is invalid
    else:
        raise Exception(f"Invalid side provided: {side}, expecting 'bid' or 'ask'.")

#Positionsüberschreitung vermeiden
def max_volume_for_pair(buy_id, sell_id, positions):
    """
    Größte Volumengröße <= base_volume, die das Positionslimit
    für beide Legs nicht sprengt.
    buy = side 'bid', sell = side 'ask'.
    """
    for vol in range(BASE_VOLUME, 0, -1):
        if (not trade_would_breach_position_limit(buy_id, vol, "bid", positions)
                and not trade_would_breach_position_limit(sell_id, vol, "ask", positions)):
            return vol
    return 0

#Pnl-Ausgabe über die konsole
def print_positions_and_pnl(positions, pnl, always_display=None):
    print("Positions:")
    for instrument_id in positions:
        if (
            not always_display
            or instrument_id in always_display
            or positions[instrument_id] != 0
        ):
            print(f"  {instrument_id:20s}: {positions[instrument_id]:4.0f}")

    if pnl is not None:
        print(f"\nPnL: {pnl:.2f}")


while True:
    positions = exchange.get_positions()
    pnl = exchange.get_pnl()
    print_positions_and_pnl(positions, pnl, always_display=[STOCK_A_ID, STOCK_B_ID])

#Auslesen derr Orderbücher
    book_a = exchange.get_last_price_book(STOCK_A_ID)
    book_b = exchange.get_last_price_book(STOCK_B_ID)

    if not (book_a and book_a.bids and book_a.asks and book_b and book_b.bids and book_b.asks):
        print("Mindestens ein Orderbuch ist leer, überspringe Iteration.")
        time.sleep(SLEEP_SECONDS)
        continue

        
    best_bid_a = book_a.bids[0]
    best_ask_a = book_a.asks[0]
    best_bid_b = book_b.bids[0]
    best_ask_b = book_b.asks[0]

    bid_a, vol_bid_a = best_bid_a.price, best_bid_a.volume
    ask_a, vol_ask_a = best_ask_a.price, best_ask_a.volume
    bid_b, vol_bid_b = best_bid_b.price, best_bid_b.volume
    ask_b, vol_ask_b = best_ask_b.price, best_ask_b.volume

    mid_a = 0.5 * (bid_a + ask_a)
    mid_b = 0.5 * (bid_b + ask_b)

    if mid_a == bid_a:
        print("mid_a == bid_a, überspringe Iteration.")
        time.sleep(SLEEP_SECONDS)
        continue


    # dynamische convergency-Schätzung aus mid_b / mid_a
    ratio = mid_b / mid_a
    #updates conversion factor slowly 90% old - 10% new
    if conv is None:
        conv = ratio
    else:
        conv = SMOOTHING * conv + (1.0 - SMOOTHING) * ratio

    print(f"Aktuelle conv-Schätzung (B ≈ conv * A): {conv:.4f}")

    # sspreads
    spread_a = ask_a - bid_a
    spread_b = ask_b - bid_b

    
    pair_spread_cost = conv * spread_a + spread_b


    EDGE_DYNAMIC = max(EDGE_BASE, 0.25 * pair_spread_cost) # half of 1 is quarter of 2 in the same unit in B


    edge_sell_b_buy_a = bid_b - conv * ask_a

    edge_buy_b_sell_a = conv * bid_a - ask_b

    print(f"Edge SELL B / BUY A : {edge_sell_b_buy_a:.4f}")
    print(f"Edge BUY  B / SELL A: {edge_buy_b_sell_a:.4f}")
    print(f"EDGE_DYNAMIC        : {EDGE_DYNAMIC:.4f}")

    traded = False


    # Case 1: PHILIPS_B zu teuer -> SELL B, BUY A
    if edge_sell_b_buy_a > EDGE_DYNAMIC:
        max_pair_vol = max_volume_for_pair(
            buy_id=STOCK_A_ID,
            sell_id=STOCK_B_ID,
            positions=positions,
        )

        # making sure the maximum volume is used 
        depth_limited_vol = min(max_pair_vol, int(min(vol_ask_a, vol_bid_b)))
        vol = max(0, depth_limited_vol)
        #
        if vol > 0:
            exchange.insert_order(
                instrument_id=STOCK_A_ID,
                price=ask_a,
                volume=vol,
                side="bid",
                order_type="ioc",
            )
            exchange.insert_order(
                instrument_id=STOCK_B_ID,
                price=bid_b,
                volume=vol,
                side="ask",
                order_type="ioc",
            )
            traded = True
        else:
            print("Arb-Signal (B teuer), aber Volumen/Positionslimit erlauben keinen Trade.")

    # Case 2: PHILIPS_B zu billig -> BUY B, SELL A
    elif edge_buy_b_sell_a > EDGE_DYNAMIC:
        max_pair_vol = max_volume_for_pair(
            buy_id=STOCK_B_ID,
            sell_id=STOCK_A_ID,
            positions=positions,
        )

        depth_limited_vol = min(max_pair_vol, int(min(vol_ask_b, vol_bid_a)))
        vol = max(0, depth_limited_vol)

        if vol > 0:
            print(
                f"Arb: B zu billig. BUY {STOCK_B_ID} @ {ask_b:.2f}, "
                f"SELL {STOCK_A_ID} @ {bid_a:.2f}, Vol {vol}, Edge {edge_buy_b_sell_a:.4f}"
            )
            exchange.insert_order(
                instrument_id=STOCK_B_ID,
                price=ask_b,
                volume=vol,
                side="bid",
                order_type="ioc",
            )
            exchange.insert_order(
                instrument_id=STOCK_A_ID,
                price=bid_a,
                volume=vol,
                side="ask",
                order_type="ioc",
            )
            traded = True
        else:
            print("Arb-Signal (B billig), aber Volumen/Positionslimit erlauben keinen Trade.")

    if not traded:
        print("Kein Trade in dieser Iteration.")

    print(f"\nSchlafen für {SLEEP_SECONDS} sekunden.")
    time.sleep(SLEEP_SECONDS)
