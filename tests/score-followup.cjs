// JSON stdin/stdout bridge used by the Python/JavaScript scoring parity test.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const root = path.join(__dirname, "..");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const window = {};
const context = vm.createContext({ window });
const appSource = fs.readFileSync(path.join(root, "static/js/app.js"), "utf8");
vm.runInContext(appSource.split("window.arfsaSetupPhotoPicker =")[0], context);
vm.runInContext(fs.readFileSync(path.join(root, "static/js/field-scoring.js"), "utf8"), context);
process.stdout.write(JSON.stringify(input.cases.map(({answers, topics}) => window.arfsaScoreFollowup(answers, topics, input.rules))));
