NOX AUTO TRADER 2.0 – MULTI-MARKET PAPER TRADING

INSTALLATION
1. Packa upp hela mappen.
2. Behåll internetanslutning.
3. Dubbelklicka START_NOX_2.bat.
4. Öppna http://localhost:8501 om webbläsaren inte öppnas.

INGÅR
- Gemensamt simulerat kapital: 500 SEK.
- Riskbudget: 1 % av aktuellt eget kapital per trade.
- Krypto: topp 10 icke-stablecoins enligt CoinPaprika, där Binance har USDT-marknadsdata.
- Aktier: 20 stora amerikanska bolag.
- Forex: EUR/USD, GBP/USD, USD/JPY.
- Råvaror: guld-, silver- och WTI-oljefutures som prisreferenser.
- Gemensam paperportfölj, simulerad avgift och stop-loss/målkurs.
- Ingen Kraken-, mäklar- eller liveorderanslutning.

VIKTIGA BEGRÄNSNINGAR
- Detta är en experimentell paper-trading-prototyp, inte en verifierad lönsam strategi.
- Yahoo Finance chart endpoint används inofficiellt för aktier, forex och råvaror. Det kan begränsas, blockeras eller ändras utan förvarning.
- Krypto använder Binance offentliga marknadsdata. CoinPaprika används för ranking.
- Data kan saknas eller vara fördröjd. Dashboarden visar då "Data saknas"; programmet ska inte skapa en signal för den marknaden.
- Forex och råvaror simuleras som prisreferenser, inte som faktisk mäklarhandel. Futures är inte samma sak som att äga den fysiska råvaran.
- En gemensam 1 % riskbudget innebär att positioner kan bli för små för att öppnas. Då avstår boten.
- Denna version använder dagliga candles för alla marknader och skannar var 15:e minut. Ny skanning ger inte ny dagsdata när marknaden är stängd.
- Den simulerade avgiften på 0,10 % per sida är ett testantagande, inte faktiska avgifter för respektive marknad.
- Pappershandel förenklar spread, slippage, marknadspåverkan, finansiering, utdelningar, futures-rollover, valutaomräkning och orderfyllnad.
- 7 dagar räcker inte för att bedöma lönsamhet. Backtest och längre framåttest krävs.
- Appen måste vara igång och datorn vaken för automatisk skanning.
- Inga lösenord, API-nycklar eller riktiga pengar behövs.

SÄKERHET
- Lägg aldrig in seed phrases, lösenord eller API-nycklar i programmet.
- Det finns ingen funktion för riktiga köp eller försäljningar.
- Paper data sparas i paper_state_v2.json i programmappen.
