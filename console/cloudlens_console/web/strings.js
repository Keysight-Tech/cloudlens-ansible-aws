// Every user-facing string. Translation later replaces this object, nothing
// else: add a sibling language key (fr, pt, de, es, hi ...) and set
// window.CLC_LANG. No user-visible text belongs anywhere but this table.
//
// Plain ES5 on purpose: loaded with a bare <script> tag, no build step.
window.CLC_STRINGS = {
  en: {
    liveBadge:        "LIVE",
    replayBadge:      "REPLAY",
    goLiveTitle:      "Run this for real",
    goLiveBody:       "Start the console on your machine, then paste its pairing code.",
    goLiveCommand:    "python3 -m cloudlens_console",
    pairPrompt:       "Pairing code",
    pairHelp:         "Eight characters, shown in the console banner.",
    pairBad:          "That code did not match. Check the console banner.",
    pairDisabled:     "The console stopped accepting codes after too many wrong " +
                      "attempts. Restart it on your machine to get a new pairing code.",
    hostRefused:      "The console refused the request as unsafe: it only answers " +
                      "to its own address on your machine. Open the console UI " +
                      "directly at http://localhost:8760.",
    pairPlaceholder:  "ABCD2345",
    connected:        "Connected to your console",
    lostConsole:      "Lost the local console. The transcript below is kept.",
    blockedPNA:       "Your browser blocked the page from reaching 127.0.0.1. " +
                      "In Chrome, enable chrome://flags/#private-network-access-respect-preflight-results, " +
                      "or open the console UI directly at http://localhost:8760.",
    waitingOnAws:     "waiting on AWS"
  }
};

// Never returns undefined: an unknown or untranslated key degrades to the key
// itself, so a gap shows up as visible text rather than an empty element.
window.CLC_T = function (key) {
  var lang = (window.CLC_LANG || "en");
  var tbl = window.CLC_STRINGS[lang] || window.CLC_STRINGS.en;
  return tbl[key] || window.CLC_STRINGS.en[key] || key;
};
