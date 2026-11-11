// The alarm clock. GitHub sometimes skips or delays its scheduled runs, so Vercel's cron (vercel.json)
// also starts the morning workflow, once a day per paper. The workflow's first step checks done.py,
// so a paper that has already gone out today is not built or sent again.
const REPO = "takky19ima/morning-edition";
const PAPERS = new Set(["kk", "my"]);

export async function GET(request) {
  const secret = process.env.CRON_SECRET;      // Vercel's cron sends it; nobody else knows it
  if (!secret || request.headers.get("authorization") !== `Bearer ${secret}`) return new Response("no", { status: 401 });
  const paper = new URL(request.url).searchParams.get("paper");
  if (!PAPERS.has(paper)) return new Response("which paper?", { status: 400 });
  const res = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/daily.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.ALARM_GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "morning-edition-alarm",
    },
    body: JSON.stringify({ ref: "main", inputs: { paper, alarm: "true" } }),
  });
  console.log(`alarm: ${paper} → GitHub ${res.status}`);
  return new Response(res.ok ? "started" : `GitHub answered ${res.status}`, { status: res.ok ? 200 : 502 });
}
