/* GenesisL1 Public API Registry — deployment configuration.
 *
 * The only file you need to edit to point the directory at your deployment.
 * It contains no secrets and is served to every visitor. Never put a private
 * key here or in any other file under web/.
 */
window.GENESIS_API_CONFIG = {
  chainId: 29,
  chainHex: "0x1d",
  chainName: "GenesisL1",
  nativeCurrency: { name: "L1 coin", symbol: "L1", decimals: 18 },
  blockExplorerUrls: ["https://explorer.genesisl1.org"],

  // Read endpoints, tried in order. The directory promotes healthy registered
  // EVM endpoints into this list at runtime, so once the register has entries
  // it stops depending on any single RPC — including the one listed here.
  rpcUrls: ["https://rpc.genesisl1.org"],

  // Deployed addresses. Both are required.
  registryAddress: "0x0000000000000000000000000000000000000000",
  lensAddress: "0x0000000000000000000000000000000000000000",

  // Telemetry feeds published by independent checkers. Each entry needs a
  // matching <url>.sig file next to it. A feed whose signature does not recover
  // to a checker the registry currently authorizes is discarded, not displayed.
  telemetryUrls: ["data/status.json"],

  // Set false only for local development against an unsigned feed.
  requireSignedTelemetry: true,

  // Instances fetched per lens call. Raise for a small register, lower if your
  // RPC caps eth_call response size.
  pageSize: 25
};
