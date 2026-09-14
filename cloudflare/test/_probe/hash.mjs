import { executionIntentHash } from "../../src/state_machine.js";
const raw={version:"R18-EXECUTION-INTENT-1",symbol:"MNQU6",side:"SELL",qty:2,orderType:"LIMIT",
 entry:"29448.50",last:null,stop:"29507.50",targets:["29400.00","29300.00"],
 legs:[{id:"TP1",qty:1,target:"29400.00"},{id:"RUNNER",qty:1,target:"29300.00"}],
 planVersion:"R19-ICT-SPLIT-1",executionContractVersion:"R22-EXECUTION-CONTRACT-1",accountScope:["LFF00000000000006"]};
console.log("js  no-pyramid :", executionIntentHash(raw));
console.log("js  pyramid-null:", executionIntentHash({...raw, pyramid: null}));
const r3={...raw, qty:4, orderType:"MARKET", entry:null, last:"29448.50",
 legs:[{id:"TP1",qty:2,target:"29400.00"},{id:"RUNNER",qty:2,target:"29300.00"}], pyramid:{baseQty:4,addQty:4}};
console.log("js  pyramid     :", executionIntentHash(r3));
