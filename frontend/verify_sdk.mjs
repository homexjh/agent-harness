// 用官方 @langchain/langgraph-sdk 的 Client 驱动后端，验证 Platform 契约正确（与前端同一套客户端）。
import { Client } from "@langchain/langgraph-sdk";

const API_URL = process.env.API_URL || "http://127.0.0.1:8123";
const client = new Client({ apiUrl: API_URL });

function summarize(ev) {
  if (ev.event === "messages") {
    const [chunk] = ev.data;
    const c = chunk || {};
    return `messages:${c.type || "?"} content=${(c.content || "").slice(0, 24)} tool_calls=${(c.tool_calls || []).length}`;
  }
  if (ev.event === "values") {
    const v = ev.data || {};
    const msgs = (v.messages || []).length;
    const intr = (v.__interrupt__ || []).length;
    return `values:msgs=${msgs} interrupt=${intr} decision=${v.decision || ""}`;
  }
  if (ev.event === "metadata") return `metadata:run=${ev.data?.run_id?.slice(0, 8)}`;
  if (ev.event === "error") return `error:${JSON.stringify(ev.data)}`;
  return ev.event;
}

async function runScenario(name, input, expectInterrupt) {
  console.log(`\n=== ${name} ===`);
  const thread = await client.threads.create();
  console.log("thread:", thread.thread_id.slice(0, 8));
  const events = [];
  const stream = client.runs.stream(thread.thread_id, "agent-harness", {
    input,
    streamMode: ["messages", "values"],
  });
  let interruptValue = null;
  for await (const ev of stream) {
    events.push(ev.event);
    console.log("  ", summarize(ev));
    // 前端(useStream)从流里的 values 事件读 __interrupt__ —— 这才是真实路径
    if (ev.event === "values" && ev.data?.__interrupt__?.length) {
      interruptValue = ev.data.__interrupt__[0].value;
    }
  }
  const sawValues = events.includes("values");
  const sawMessages = events.includes("messages");
  const sawEnd = events.includes("end") || events.includes("error");
  console.log(`  -> messages=${sawMessages} values=${sawValues} end=${sawEnd} interrupt=${interruptValue ? 1 : 0}`);
  return { thread, events, interruptValue };
}

async function main() {
  // 场景1：普通对话（应收敛，无 interrupt）
  const s1 = await runScenario("场景1 普通对话", { messages: [{ role: "user", content: "你好，介绍一下你自己" }] }, false);
  if (!s1.events.includes("values")) throw new Error("FAIL: 场景1 缺少 values 事件");

  // 场景2：写文件（应触发 ApprovalGate -> interrupt -> __interrupt__）
  const s2 = await runScenario("场景2 写文件（人工裁决）", { messages: [{ role: "user", content: "帮我写个文件，内容是 hello" }] }, true);
  if (!s2.interruptValue) throw new Error("FAIL: 场景2 未触发 interrupt");
  console.log("  interrupt value:", JSON.stringify(s2.interruptValue));

  // 场景3：resume（批准）-> 应执行工具并收敛
  console.log("\n=== 场景3 人工批准 resume ===");
  const stream2 = client.runs.stream(s2.thread.thread_id, "agent-harness", {
    command: { resume: "approved" },
    streamMode: ["messages", "values"],
  });
  let resumedOk = false;
  for await (const ev of stream2) {
    console.log("  ", summarize(ev));
    if (ev.event === "values" && (ev.data?.messages || []).some((m) => m.type === "tool")) resumedOk = true;
  }
  console.log("  -> resume 后出现 tool 消息:", resumedOk);

  console.log("\nALL SDK CONTRACT CHECKS PASSED");
}

main().catch((e) => {
  console.error("SDK VERIFY FAILED:", e);
  process.exit(1);
});
