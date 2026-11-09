// SVG → 1024 PNG 母版渲染（resvg-js，纯矢量光栅化，不做位图放大）
// 用法：node render.js [svg文件名]   默认渲染 src/candles-ascending.svg
const fs = require("fs");
const path = require("path");
const { Resvg } = require("@resvg/resvg-js");

const here = __dirname;
const name = process.argv[2] || "candles-ascending.svg";
const svg = fs.readFileSync(path.join(here, "src", name));
const out = path.join(here, "master_1024.png");
const r = new Resvg(svg, { fitTo: { mode: "width", value: 1024 } });
fs.writeFileSync(out, r.render().asPng());
console.log(out);
