// Genera fixtures/seed.json dal seed JS: il bootstrap prende JSON.
import { writeFileSync } from "node:fs";
const { DATI } = await import("../handoff/inventario.js");
writeFileSync("fixtures/seed.json", JSON.stringify(DATI, null, 2) + "\n", "utf8");
console.log("seed.json scritto:", Object.keys(DATI).join(", "));
