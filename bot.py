async def risk_loop(chat_id: int):
    await asyncio.sleep(2)
    print("[LOOP] risk_loop started")

    while chat_id in active_chats:
        for symbol in SYMBOLS:
            try:
                f = funding.get(symbol)
                oi = open_interest.get(symbol)
                ls = long_short_ratio.get(symbol)
                liq = liquidations.get(symbol, 0)

                # 🔹 БАЗОВОЕ ОБНОВЛЕНИЕ CACHE ВСЕГДА
                cache[symbol] = (0, None, ["Данные собираются"])
                print(f"[CACHE] touch {symbol} f={f} oi={oi} ls={ls}")

                if f is None or oi is None or ls is None:
                    continue

                long_ratio = ls["long"] / max(ls["long"] + ls["short"], 1)

                prev_oi = last_oi.get(symbol, oi)
                oi_change = oi - prev_oi
                last_oi[symbol] = oi

                prev_funding = last_funding.get(symbol)
                last_funding[symbol] = f

                score, direction, reasons, funding_spike, oi_spike = calculate_risk(
                    f,
                    prev_funding,
                    long_ratio,
                    oi_change,
                    oi,
                    liq
                )

                cache[symbol] = (score, direction, reasons)
                print(f"[CACHE] updated {symbol} score={score}")

            except Exception as e:
                print("[BOT ERROR]", e)

        await asyncio.sleep(INTERVAL_SECONDS)
