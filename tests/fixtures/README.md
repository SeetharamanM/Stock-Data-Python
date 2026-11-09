# Test fixtures

Recorded tool results used by the tests (market data only, no account data).

* `indmoney_*` and `trendlyne_*` are real responses from the INDmoney and Trendlyne
  MCP servers (9 Nov 2026). `trendlyne_news_infy.txt` keeps 5 of the 20 records
  and shortens the one long HTML description.
* `kite_*` are constructed from the Kite MCP server source
  (github.com/zerodha/kite-mcp-server) and the Kite Connect API formats.
