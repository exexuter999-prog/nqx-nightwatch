# NQ Nightwatch 🩹 Nerf Edition

MNQ(マイクロ NASDAQ 先物)の 3 分足を TradingView から取り、構造で候補を採点して
CrossTrade 経由で発注・管理する自律運用システムの **公開版(補助輪版)** です。

- 中身と運用契約: [CLAUDE.md](CLAUDE.md)(監視 PC 上のエージェント向け)/ [AGENTS.md](AGENTS.md)(外部エージェント向け)
- **この版は意図的に弱体化してあります**: [NERF.md](NERF.md)。走らせると `🩹power 14%` と自己申告します。
- フルパワー版(R125 以降の全部)は作者の PC の中だけで動いています。使いたい人は作者へ: <https://github.com/exexuter999-prog>

```powershell
python nerf.py          # いま何が封印されているか
python nqx_cycle.py     # 1 周期(取得 → 評価 → publish)。窓の外では停止中の 1 行だけ
```

免責: 先物取引は元本を失う可能性があります。この公開版は教材であり、作者は結果に責任を持ちません。
