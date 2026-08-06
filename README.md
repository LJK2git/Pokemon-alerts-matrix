# Pokemon-alerts-matrix
gets all info from reddit
Built for matrix since messages are faster than discord and sometimes notifications from discord dont appear on iphones(maybe androids too idk). Matrix solves all of these issues, I recommend to use element x as your matrix client.

bot.py runs the matrix bot

monitor.py runs a script pulling new info from a reddit community and searches through it for new items, it currently supports target drops, bestbuy drops, and pokemon center drops.

tcin.py is used by monitor.py to find the link of a official product from target when it is found by the monitor.(finding a link may fail if this happens search for the product like this "!search product name" it will send the most likely links) tcin.py also searches for only official products meaning products from third party sellers will be blocked out. Manually searching a product on target is impossible as they will only show you third party sellers and not their official products so i recommend to always use the !search command when automatic link finding fails.

config.json is the config for the keywords the monitor searches for and you can block out certain products or sets like pitch black or chaos rising so you wont get alerts for them.

To install copy the repo, add your matrix info in secrets.json, you may also change config.json to your prefrences, install docker, and do "docker compose up -d --build"
