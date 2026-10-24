import { useMemo, useState } from "react";
import type { KernelClient } from "@llmchip/client";
import type { RunRecord } from "@llmchip/kernel-api";
import { useKernel, useQuery } from "../../api.ts";
import { useI18n, type Key } from "../../i18n.ts";
import { ErrorLine, Section } from "../common.tsx";
import {
  adoptScheme,
  runScheme,
  type EvalSize,
  type Scheme,
  type StepState,
} from "../modelPipeline.ts";
import { useModelChoices } from "./S1.tsx";

// S2 in detail: the numeric scheme of the chip (weight bits, activation formats, the
// accumulator, rounding and overflow) explained from the numeric spec itself; the
// accuracy of the integer model L2 against L0; a test bench that runs another scheme;
// and every scheme tried, side by side. A project with a llama2.c Model IR has the chip
// path (the formal numeric spec, carried to the RTL); any recovered graph has the
// analysis path (numeric/quantize_graph), for models that do not become a chip.

type Json = any;
type Path = "chip" | "graph";

const QUANTIZE: Record<string, Path> = {
  quantize: "chip",
  quantize_graph: "graph",
};
const EVALUATE: Record<string, Path> = {
  evaluate: "chip",
  evaluate_graph: "graph",
};
const CHECK: Record<Path, string> = {
  chip: "accuracy.budget",
  graph: "numeric.accuracy",
};
const FORMATS: Scheme["weightFormat"][] = ["int8", "int4", "int3", "ternary"];
const GROUPS = [16, 32, 64, 128];
const ROUNDINGS: Scheme["rounding"][] = ["half-up", "half-even", "truncate"];
const OVERFLOWS: Scheme["overflow"][] = ["saturate", "wrap"];
const KIND_KEYS: Record<string, Key> = {
  x: "s2.kind.x",
  xn: "s2.kind.xn",
  q: "s2.kind.q",
  k: "s2.kind.k",
  qn: "s2.kind.qn",
  kn: "s2.kind.kn",
  v: "s2.kind.v",
  h1: "s2.kind.h1",
  h3: "s2.kind.h3",
  g: "s2.kind.g",
  logits: "s2.kind.logits",
  score: "s2.kind.score",
  att: "s2.kind.att",
  norm: "s2.kind.norm",
  accumulator: "s2.kind.accumulator",
};
const RUN_STEPS: [string, Key][] = [
  ["quantize", "s2.run.quantize"],
  ["evaluate", "s2.run.evaluate"],
  ["check", "s2.run.check"],
  ["bind", "s2.run.bind"],
  ["budget", "s2.run.budget"],
  ["infer", "s2.run.infer"],
];

async function json(
  client: KernelClient,
  hash: string | undefined,
): Promise<Json> {
  return hash ? (await client.download(hash)).json() : undefined;
}

const out = (r: RunRecord | undefined, name: string) =>
  r?.outputs.find((o) => o.name === name)?.hash;
const inp = (r: RunRecord | undefined, name: string) =>
  r?.inputs.find((i) => i.name === name)?.hash;

/** One scheme that was evaluated: its quantize and evaluate runs. */
interface Row {
  path: Path;
  spec: string;
  quantize: RunRecord | undefined;
  evaluate: RunRecord;
}

/** The scheme of a quantize run, from its parameters (defaults as the plugin applies them). */
function schemeOf(q: RunRecord | undefined, path: Path): Scheme {
  const p = (q?.params ?? {}) as Partial<Scheme>;
  return {
    weightFormat: p.weightFormat ?? (path === "chip" ? "int4" : "int8"),
    groupSize: p.groupSize ?? 32,
    rounding: p.rounding ?? "half-up",
    overflow: p.overflow ?? "saturate",
    ...(p.accumulatorBits ? { accumulatorBits: p.accumulatorBits } : {}),
    ...(path === "graph"
      ? { perLayer: p.perLayer ?? true, residualBits: p.residualBits ?? 24 }
      : {}),
  };
}

function useFormat() {
  const { lang } = useI18n();
  return useMemo(() => {
    const locale = lang === "zh" ? "zh-CN" : "en-US";
    const num = (v: number, digits = 2) =>
      new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(
        v,
      );
    return {
      int: (v: number) => new Intl.NumberFormat(locale).format(v),
      num,
      pct: (v: number) => `${num(100 * v, 1)}%`,
      sci: (v: number) =>
        v === 0
          ? "0"
          : Math.abs(v) < 1e-3 || Math.abs(v) >= 1e5
            ? v.toExponential(2)
            : num(v, 4),
      bytes: (bits: number) =>
        bits >= 8e9
          ? `${num(bits / 8e9)} GB`
          : bits >= 8e6
            ? `${num(bits / 8e6)} MB`
            : `${num(bits / 8e3)} KB`,
    };
  }, [lang]);
}

/** "int4 · 分组 32 · …": a scheme in one line. */
function useSchemeText() {
  const { t } = useI18n();
  return (s: Scheme, accBits?: number) =>
    [
      t("s2.scheme.weights", { format: s.weightFormat, group: s.groupSize }),
      accBits
        ? t("s2.scheme.acc", { bits: accBits })
        : s.accumulatorBits
          ? t("s2.scheme.acc", { bits: s.accumulatorBits })
          : t("s2.scheme.accAuto"),
      t(`s2.round.${s.rounding}` as Key),
      t(`s2.ovf.${s.overflow}` as Key),
      ...(s.perLayer !== undefined
        ? [
            t(s.perLayer ? "s2.scheme.perLayer" : "s2.scheme.perKind"),
            t("s2.scheme.residual", { bits: s.residualBits }),
          ]
        : []),
    ].join(" · ");
}

export function S2Detail({
  project,
  onBack,
  onProject,
}: {
  project: string;
  onBack(): void;
  onProject(project: string): void;
}) {
  const { t, message } = useI18n();
  const { client } = useKernel();
  const { data: runsData } = useQuery(
    (c) => c.call("runs.list", { project, limit: 1000 }),
    [project],
  );
  const { data: state } = useQuery(
    (c) => c.call("projects.get", { project }),
    [project],
  );
  const { data: verdicts } = useQuery(
    (c) => c.call("verdicts.list", { limit: 1000 }),
    [],
  );
  const choices = useModelChoices();
  const runs = runsData?.runs ?? [];
  const okRuns = runs.filter((r) => r.execution === "exited_zero");
  const quantizes = okRuns.filter(
    (r) => r.plugin === "numeric" && QUANTIZE[r.handler],
  );
  const rows: Row[] = okRuns
    .filter((r) => r.plugin === "numeric" && EVALUATE[r.handler])
    .map((e) => {
      const spec = inp(e, "spec")!;
      return {
        path: EVALUATE[e.handler]!,
        spec,
        quantize: quantizes.find((q) => out(q, "spec") === spec),
        evaluate: e,
      };
    });
  const hasChip = okRuns.some(
    (r) => r.plugin === "model-llama" && r.handler === "intake",
  );
  const hasGraph = okRuns.some(
    (r) =>
      r.plugin === "model-llama" &&
      r.handler === "recover" &&
      r.metrics.family_supported === true,
  );
  const formal = state?.slots["numeric-spec"];
  const [picked, setPicked] = useState<string | null>(null);
  const selected =
    picked &&
    (rows.some((r) => r.spec === picked) ||
      quantizes.some((q) => out(q, "spec") === picked))
      ? picked
      : (formal ?? rows.at(-1)?.spec ?? out(quantizes.at(-1), "spec") ?? null);
  const row = [...rows].reverse().find((r) => r.spec === selected);
  const quantize =
    row?.quantize ?? quantizes.find((q) => out(q, "spec") === selected);
  const path: Path =
    row?.path ??
    (quantize ? QUANTIZE[quantize.handler]! : hasChip ? "chip" : "graph");
  const { data: spec } = useQuery(
    (c) => json(c, selected ?? undefined),
    [selected],
    false,
  );
  const { data: report } = useQuery(
    (c) => json(c, out(row?.evaluate, "report")),
    [out(row?.evaluate, "report")],
    false,
  );
  const verdictOf = (check: string, r: RunRecord | undefined) =>
    verdicts?.verdicts
      .filter((v) => v.check === check && v.run === r?.id)
      .at(-1);

  return (
    <>
      <button className="link step-back" onClick={onBack}>
        ← {t("s0.back")}
      </button>
      <Section title={t("s2.title")}>
        <p className="flow-what">{t("s2.intro")}</p>
        <h4>{t("s2.pick")}</h4>
        <div className="s1-picker">
          {!choices.some((c) => c.project === project) && (
            <button className="active">{t("s1.pickOwn")}</button>
          )}
          {choices.map((c) => (
            <button
              key={c.project}
              className={c.project === project ? "active" : ""}
              onClick={() => onProject(c.project)}
              title={c.project}
            >
              {c.label}
            </button>
          ))}
        </div>
        <p className="muted">
          {t(
            hasChip ? "s2.pathChip" : hasGraph ? "s2.pathGraph" : "s2.pathNone",
          )}
        </p>
        {spec && (
          <Answer
            spec={spec}
            report={report}
            quantize={quantize}
            path={path}
            formal={selected === formal}
          />
        )}
      </Section>

      {(hasChip || hasGraph) && (
        <Bench
          project={project}
          hasChip={hasChip}
          hasGraph={hasGraph}
          onDone={(s) => setPicked(s)}
        />
      )}
      {rows.length > 0 && (
        <Comparison
          rows={rows}
          selected={selected}
          formal={formal}
          onSelect={setPicked}
          verdictOf={verdictOf}
        />
      )}

      {spec && (
        <>
          {report && (
            <Accuracy
              report={report}
              path={path}
              verdict={verdictOf(CHECK[path], row?.evaluate)}
              message={message}
            />
          )}
          <Weights spec={spec} quantize={quantize} />
          <Activations spec={spec} />
          <Accumulator spec={spec} />
          <RoundingOverflow spec={spec} />
          <Tables spec={spec} />
          {path === "chip" && row && quantize && selected !== formal && (
            <Adopt
              project={project}
              quantize={quantize}
              evaluate={row.evaluate}
            />
          )}
        </>
      )}
    </>
  );
}

/** The scheme and its result in one paragraph. */
function Answer({
  spec,
  report,
  quantize,
  path,
  formal,
}: {
  spec: Json;
  report: Json;
  quantize: RunRecord | undefined;
  path: Path;
  formal: boolean;
}) {
  const { t } = useI18n();
  const f = useFormat();
  const text = useSchemeText();
  const m = report?.metrics;
  return (
    <div className="s0-answer">
      <span>
        <span className="pill">
          {t(
            path === "chip"
              ? formal
                ? "s2.formal"
                : "s2.chipCandidate"
              : "s2.analysis",
          )}
        </span>{" "}
        {text(schemeOf(quantize, path), spec.accumulator.bits)}
      </span>
      <small>
        {t("s2.answerAct", { act: actFormat(spec, t) })}{" "}
        {t(
          spec.accumulator.canOverflow
            ? "s2.answerAccNarrow"
            : "s2.answerAccSafe",
          {
            bits: spec.accumulator.bits,
            auto: spec.accumulator.autoBits ?? spec.accumulator.bits,
          },
        )}{" "}
        {m
          ? t("s2.answerAccuracy", {
              ppl0: f.num(m.l0_perplexity),
              ppl2: f.num(m.l2_perplexity),
              delta: `${m.ppl_increase_pct > 0 ? "+" : ""}${f.num(m.ppl_increase_pct, 1)}%`,
              top1:
                m.teacher_forced_top1 !== undefined
                  ? f.pct(m.teacher_forced_top1)
                  : "—",
            })
          : t("s2.answerNoEval")}
      </small>
    </div>
  );
}

function actFormat(
  spec: Json,
  t: (k: Key, p?: Record<string, unknown>) => string,
): string {
  const a = spec.activations;
  const residual = a.bits?.x ?? 16;
  return [
    t("s2.act.int16"),
    residual !== 16 ? t("s2.act.residual", { bits: residual }) : "",
    t(a.granularity === "layer" ? "s2.act.perLayer" : "s2.act.perKind"),
  ]
    .filter(Boolean)
    .join(t("s2.listSep"));
}

/** The test bench: another scheme on the chip path or on the recovered graph. */
function Bench({
  project,
  hasChip,
  hasGraph,
  onDone,
}: {
  project: string;
  hasChip: boolean;
  hasGraph: boolean;
  onDone(spec: string): void;
}) {
  const { t } = useI18n();
  const { client } = useKernel();
  const [path, setPath] = useState<Path>(hasChip ? "chip" : "graph");
  const [scheme, setScheme] = useState<Scheme>({
    weightFormat: hasChip ? "int4" : "int8",
    groupSize: 32,
    rounding: "half-up",
    overflow: "saturate",
    perLayer: true,
    residualBits: 24,
  });
  const [accAuto, setAccAuto] = useState(true);
  const [accBits, setAccBits] = useState(32);
  const [size, setSize] = useState<EvalSize>({
    maxPrompts: 4,
    steps: 8,
    passageTokens: 128,
  });
  const [progress, setProgress] = useState<
    Record<string, { state: StepState; detail?: string }>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (change: Partial<Scheme>) =>
    setScheme((s) => ({ ...s, ...change }));
  const effective =
    path === "chip" && !hasChip
      ? "graph"
      : path === "graph" && !hasGraph
        ? "chip"
        : path;
  const go = async () => {
    setBusy(true);
    setError(null);
    setProgress({});
    try {
      const chosen: Scheme = {
        ...scheme,
        ...(accAuto
          ? { accumulatorBits: undefined }
          : { accumulatorBits: accBits }),
      };
      const { quantize } = await runScheme(
        client,
        project,
        effective,
        chosen,
        size,
        (step, state, detail) =>
          setProgress((p) => ({
            ...p,
            [step]: { state, ...(detail ? { detail } : {}) },
          })),
      );
      onDone(quantize.outputs.find((o) => o.name === "spec")!.hash);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title={t("s2.bench")}>
      <p className="flow-what">{t("s2.benchIntro")}</p>
      {hasChip && hasGraph && (
        <div className="s1-picker">
          {(["chip", "graph"] as const).map((p) => (
            <button
              key={p}
              className={effective === p ? "active" : ""}
              onClick={() => setPath(p)}
            >
              {t(p === "chip" ? "s2.onChip" : "s2.onGraph")}
            </button>
          ))}
        </div>
      )}
      <div className="form grid s2-bench">
        <label>
          {t("s2.in.format")}
          <select
            value={scheme.weightFormat}
            onChange={(e) =>
              set({ weightFormat: e.target.value as Scheme["weightFormat"] })
            }
          >
            {FORMATS.map((x) => (
              <option key={x} value={x}>
                {t(`s2.format.${x}` as Key)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("s2.in.group")}
          <select
            value={scheme.groupSize}
            onChange={(e) => set({ groupSize: Number(e.target.value) })}
          >
            {GROUPS.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("s2.in.rounding")}
          <select
            value={scheme.rounding}
            onChange={(e) =>
              set({ rounding: e.target.value as Scheme["rounding"] })
            }
          >
            {ROUNDINGS.map((x) => (
              <option key={x} value={x}>
                {t(`s2.round.${x}` as Key)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("s2.in.overflow")}
          <select
            value={scheme.overflow}
            onChange={(e) =>
              set({ overflow: e.target.value as Scheme["overflow"] })
            }
          >
            {OVERFLOWS.map((x) => (
              <option key={x} value={x}>
                {t(`s2.ovf.${x}` as Key)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("s2.in.acc")}
          <span className="s2-acc">
            <select
              value={accAuto ? "auto" : "fixed"}
              onChange={(e) => setAccAuto(e.target.value === "auto")}
            >
              <option value="auto">{t("s2.in.accAuto")}</option>
              <option value="fixed">{t("s2.in.accFixed")}</option>
            </select>
            {!accAuto && (
              <input
                type="number"
                min={16}
                max={64}
                value={accBits}
                onChange={(e) => setAccBits(Number(e.target.value))}
              />
            )}
          </span>
        </label>
        {effective === "graph" && (
          <>
            <label>
              {t("s2.in.granularity")}
              <select
                value={scheme.perLayer ? "layer" : "kind"}
                onChange={(e) => set({ perLayer: e.target.value === "layer" })}
              >
                <option value="layer">{t("s2.act.perLayer")}</option>
                <option value="kind">{t("s2.act.perKind")}</option>
              </select>
            </label>
            <label>
              {t("s2.in.residual")}
              <select
                value={scheme.residualBits}
                onChange={(e) =>
                  set({ residualBits: Number(e.target.value) as 16 | 24 })
                }
              >
                <option value={24}>24 bit</option>
                <option value={16}>16 bit</option>
              </select>
            </label>
            <label>
              {t("s2.in.prompts")}
              <input
                type="number"
                min={1}
                max={8}
                value={size.maxPrompts}
                onChange={(e) =>
                  setSize({ ...size, maxPrompts: Number(e.target.value) })
                }
              />
            </label>
            <label>
              {t("s2.in.steps")}
              <input
                type="number"
                min={1}
                max={32}
                value={size.steps}
                onChange={(e) =>
                  setSize({ ...size, steps: Number(e.target.value) })
                }
              />
            </label>
            <label>
              {t("s2.in.passage")}
              <input
                type="number"
                min={32}
                max={240}
                value={size.passageTokens}
                onChange={(e) =>
                  setSize({ ...size, passageTokens: Number(e.target.value) })
                }
              />
            </label>
          </>
        )}
        <button className="active" disabled={busy} onClick={go}>
          {busy ? t("playground.running") : t("s2.runScheme")}
        </button>
      </div>
      <p className="muted">
        {t(effective === "chip" ? "s2.benchChipNote" : "s2.benchGraphNote")}
      </p>
      <Progress progress={progress} />
      <ErrorLine error={error} />
    </Section>
  );
}

function Progress({
  progress,
}: {
  progress: Record<string, { state: StepState; detail?: string }>;
}) {
  const { t } = useI18n();
  if (Object.keys(progress).length === 0) return null;
  return (
    <ul className="s1-progress">
      {RUN_STEPS.filter(([id]) => progress[id]).map(([id, label]) => (
        <li key={id} className={progress[id]!.state}>
          {progress[id]!.state === "running"
            ? "…"
            : progress[id]!.state === "done"
              ? "✓"
              : "✗"}{" "}
          {t(label)}
          {progress[id]!.detail && (
            <small className="muted">
              {" "}
              {["pass", "fail", "indeterminate"].includes(progress[id]!.detail!)
                ? t(`flow.verdict.${progress[id]!.detail}` as Key)
                : progress[id]!.detail}
            </small>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Every evaluated scheme of the project, side by side. */
function Comparison({
  rows,
  selected,
  formal,
  onSelect,
  verdictOf,
}: {
  rows: Row[];
  selected: string | null;
  formal: string | undefined;
  onSelect(spec: string): void;
  verdictOf(check: string, r: RunRecord): { verdict: string } | undefined;
}) {
  const { t } = useI18n();
  const f = useFormat();
  const text = useSchemeText();
  return (
    <Section title={t("s2.compare")}>
      <p className="flow-what">{t("s2.compareIntro")}</p>
      <div className="s2-scroll">
        <table className="s0-table s2-compare">
          <thead>
            <tr>
              <th>{t("s2.col.scheme")}</th>
              <th>{t("s2.col.storage")}</th>
              <th>{t("s2.col.ppl")}</th>
              <th>{t("s2.col.top1")}</th>
              <th>{t("s2.col.greedy")}</th>
              <th>{t("s2.col.overflows")}</th>
              <th>{t("s2.col.check")}</th>
            </tr>
          </thead>
          <tbody>
            {[...rows].reverse().map((r) => {
              const m = r.evaluate.metrics as Record<string, number> & {
                l0_perplexity: number;
                l2_perplexity: number;
                ppl_increase_pct: number;
                token_agreement: number;
              };
              const qm = (r.quantize?.metrics ?? {}) as Record<string, number>;
              const storage =
                (qm["storage_bits.weights"] ?? 0) +
                (qm["storage_bits.scales"] ?? 0);
              const v = verdictOf(CHECK[r.path], r.evaluate);
              return (
                <tr
                  key={r.evaluate.id}
                  className={`clickable${r.spec === selected ? " selected" : ""}`}
                  onClick={() => onSelect(r.spec)}
                >
                  <td>
                    <span className="pill">
                      {t(
                        r.path === "chip"
                          ? r.spec === formal
                            ? "s2.formal"
                            : "s2.chipCandidate"
                          : "s2.analysis",
                      )}
                    </span>{" "}
                    {text(schemeOf(r.quantize, r.path), qm.accumulator_bits)}
                  </td>
                  <td>{storage ? f.bytes(storage) : "—"}</td>
                  <td>
                    {f.num(m.l0_perplexity)} → {f.num(m.l2_perplexity)}{" "}
                    <small
                      className={m.ppl_increase_pct > 20 ? "error" : "muted"}
                    >
                      ({m.ppl_increase_pct > 0 ? "+" : ""}
                      {f.num(m.ppl_increase_pct, 1)}%)
                    </small>
                  </td>
                  <td>
                    {m.teacher_forced_top1 !== undefined
                      ? f.pct(m.teacher_forced_top1)
                      : "—"}
                  </td>
                  <td>{f.pct(m.token_agreement)}</td>
                  <td>{f.int(m.saturations ?? 0)}</td>
                  <td>
                    {v ? (
                      <span
                        className={`pill ${v.verdict === "pass" ? "ok" : "bad"}`}
                      >
                        {t(`flow.verdict.${v.verdict}` as Key)}
                      </span>
                    ) : (
                      <span className="muted">{t("s2.notJudged")}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function Accuracy({
  report,
  path,
  verdict,
  message,
}: {
  report: Json;
  path: Path;
  verdict:
    | {
        verdict: string;
        reasons: { code: string; params?: Record<string, unknown> }[];
      }
    | undefined;
  message: (m: { code: string; params?: Record<string, unknown> }) => string;
}) {
  const { t } = useI18n();
  const f = useFormat();
  const m = report.metrics;
  const worst = Math.max(
    ...(report.layerError ?? []).map((r: Json) => r.maxRel),
    1e-12,
  );
  const after = (report.layerError ?? []).filter(
    (r: Json) => r.point === "ffn_residual",
  );
  const kinds = Object.entries(
    (report.saturationsByKind ?? {}) as Record<string, number>,
  );
  return (
    <Section title={t("s2.accuracy")}>
      <p className="flow-what">
        {t(path === "chip" ? "s2.accuracyChip" : "s2.accuracyGraph", {
          prompts: m.prompts_evaluated,
          steps: m.tokens_per_prompt,
          tokens: m.passage_tokens,
        })}
      </p>
      <div className="flow-facts">
        <Fact
          label="s2.fact.ppl"
          value={`${f.num(m.l0_perplexity)} → ${f.num(m.l2_perplexity)} (${m.ppl_increase_pct > 0 ? "+" : ""}${f.num(m.ppl_increase_pct, 1)}%)`}
          good={m.ppl_increase_pct <= 20}
        />
        {m.teacher_forced_top1 !== undefined && (
          <Fact
            label="s2.fact.top1"
            value={f.pct(m.teacher_forced_top1)}
            good={m.teacher_forced_top1 >= 0.9}
          />
        )}
        <Fact label="s2.fact.greedy" value={f.pct(m.token_agreement)} />
        <Fact
          label="s2.fact.divergence"
          value={f.num(m.first_divergence_mean, 1)}
        />
        {m.layer_error_max_rel !== undefined && (
          <Fact
            label="s2.fact.layerError"
            value={f.pct(m.layer_error_max_rel)}
          />
        )}
        <Fact
          label="s2.fact.overflows"
          value={f.int(m.saturations ?? 0)}
          good={(m.saturations ?? 0) === 0}
        />
      </div>
      <ul className="flow-checks">
        <li>
          <span
            className={`pill ${verdict?.verdict === "pass" ? "ok" : verdict ? "bad" : ""}`}
          >
            {t(`flow.verdict.${verdict?.verdict ?? "none"}` as Key)}
          </span>
          {t(
            path === "chip"
              ? "flow.check.accuracy"
              : "flow.check.numericAccuracy",
          )}
          {verdict && verdict.verdict !== "pass" && (
            <small className="muted">
              {" "}
              · {verdict.reasons.map(message).join("; ")}
            </small>
          )}
          {!verdict && path === "chip" && (
            <small className="muted"> · {t("s2.budgetOnAdopt")}</small>
          )}
        </li>
      </ul>
      {after.length > 0 && (
        <>
          <h4>{t("s2.layerError")}</h4>
          <p className="muted">{t("s2.layerErrorNote")}</p>
          <div className="s2-bars">
            {after.map((r: Json) => (
              <div key={r.layer} className="s2-bar">
                <small>{t("s2.layerN", { n: r.layer })}</small>
                <span
                  style={{ width: `${Math.max(1, (100 * r.maxRel) / worst)}%` }}
                />
                <small>{f.pct(r.maxRel)}</small>
              </div>
            ))}
          </div>
        </>
      )}
      <h4>{t("s2.overflowsByKind")}</h4>
      {kinds.length === 0 ? (
        <p className="muted">{t("s2.noOverflows")}</p>
      ) : (
        <table className="s0-table">
          <tbody>
            {kinds.map(([k, n]) => (
              <tr key={k}>
                <td>{t(KIND_KEYS[k] ?? "s2.kind.other")}</td>
                <td>{f.int(n)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h4>{t("s2.samples")}</h4>
      <table className="s0-table s2-wrap">
        <thead>
          <tr>
            <th>{t("s1.col.prompt")}</th>
            <th>L0</th>
            <th>L2</th>
            <th>{t("s2.col.same")}</th>
          </tr>
        </thead>
        <tbody>
          {(report.samples ?? []).slice(0, 8).map((s: Json, i: number) => (
            <tr key={i}>
              <td>{s.prompt}</td>
              <td className="generated">{s.l0}</td>
              <td className="generated">{s.l2}</td>
              <td>{f.pct(s.agreement)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Fact({
  label,
  value,
  good,
}: {
  label: Key;
  value: string;
  good?: boolean;
}) {
  const { t } = useI18n();
  return (
    <div
      className={`flow-fact ${good === true ? "ok" : good === false ? "bad" : ""}`}
    >
      <small>{t(label)}</small>
      <strong>{value}</strong>
    </div>
  );
}

/** Weights: format, the worked example of a real row, and what the chip stores. */
function Weights({
  spec,
  quantize,
}: {
  spec: Json;
  quantize: RunRecord | undefined;
}) {
  const { t } = useI18n();
  const f = useFormat();
  const w = spec.weights;
  const ex = spec.example;
  const bits = Object.entries(
    (quantize?.metrics ?? {}) as Record<string, number>,
  ).filter(([k]) => k.startsWith("storage_bits."));
  const total = bits.reduce((a, [, v]) => a + v, 0);
  return (
    <Section title={t("s2.weights")}>
      <p className="flow-what">
        {t(w.method === "absmean" ? "s2.weightsTernary" : "s2.weightsRtn", {
          bits: w.storageBits,
          levels: `${w.levels[0]} … ${w.levels[1]}`,
          group: w.groupSize,
        })}{" "}
        {t("s2.weightsScale")}
      </p>
      {ex && (
        <>
          <h4>{t("s2.example", { tensor: ex.tensor, n: ex.values.length })}</h4>
          <p className="s0-formula">
            {t("s2.exampleScale", {
              m: ex.mantissa,
              e: ex.exponent,
              scale: f.sci(ex.scale),
            })}
          </p>
          <table className="s0-table s2-example">
            <tbody>
              <tr>
                <th>{t("s2.ex.float")}</th>
                {ex.values.map((v: number, i: number) => (
                  <td key={i}>{f.sci(v)}</td>
                ))}
              </tr>
              <tr>
                <th>{t("s2.ex.level")}</th>
                {ex.levels.map((v: number, i: number) => (
                  <td key={i}>
                    <strong>{v}</strong>
                  </td>
                ))}
              </tr>
              <tr>
                <th>{t("s2.ex.back")}</th>
                {ex.reconstructed.map((v: number, i: number) => (
                  <td key={i}>{f.sci(v)}</td>
                ))}
              </tr>
            </tbody>
          </table>
          <p className="muted">
            {t("s2.exampleError", {
              error: f.sci(ex.maxError),
              half: f.sci(ex.scale / 2),
            })}
          </p>
        </>
      )}
      {total > 0 && (
        <>
          <h4>{t("s2.storage", { total: f.bytes(total) })}</h4>
          <table className="s0-table">
            <tbody>
              {bits.map(([k, v]) => (
                <tr key={k}>
                  <td>
                    {t(`s2.store.${k.slice("storage_bits.".length)}` as Key)}
                  </td>
                  <td>{f.bytes(v)}</td>
                  <td>{f.pct(v / total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Section>
  );
}

const ORDER = ["x", "xn", "q", "k", "qn", "kn", "v", "h1", "h3", "g"];

/** Activations: each tensor kind's format, from the calibrated largest value. */
function Activations({ spec }: { spec: Json }) {
  const { t } = useI18n();
  const f = useFormat();
  const a = spec.activations;
  const maxAbs = (a.calibration?.maxAbs ?? {}) as Record<string, number>;
  const kinds = ORDER.filter(
    (k) =>
      a.frac[k] !== undefined &&
      (maxAbs[k] !== undefined || spec.format.endsWith("spec/1")),
  );
  const layers = (a.layerFrac ?? []) as Record<string, number>[];
  return (
    <Section title={t("s2.activations")}>
      <p className="flow-what">
        {t(a.granularity === "layer" ? "s2.actIntroLayer" : "s2.actIntroKind")}
      </p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t("s2.col.kind")}</th>
            <th>{t("s2.col.max")}</th>
            <th>{t("s2.col.bits")}</th>
            <th>{t("s2.col.frac")}</th>
            <th>{t("s2.col.resolution")}</th>
            <th>{t("s2.col.range")}</th>
          </tr>
        </thead>
        <tbody>
          {kinds.map((k) => {
            const bits = a.bits?.[k] ?? 16;
            const fr = a.frac[k] as number;
            const perLayer =
              k !== "x" && layers.length > 0 ? layers.map((l) => l[k]!) : null;
            return (
              <tr key={k}>
                <td>{t(KIND_KEYS[k]!)}</td>
                <td>{maxAbs[k] !== undefined ? f.num(maxAbs[k]!, 3) : "—"}</td>
                <td>{bits}</td>
                <td>
                  {perLayer
                    ? t("s2.fracRange", {
                        lo: Math.min(...perLayer),
                        hi: Math.max(...perLayer),
                      })
                    : fr}
                </td>
                <td>{perLayer ? "—" : f.sci(2 ** -fr)}</td>
                <td>{perLayer ? "—" : `±${f.num(2 ** (bits - 1 - fr), 3)}`}</td>
              </tr>
            );
          })}
          <tr>
            <td>{t("s2.kind.logits")}</td>
            <td>
              {maxAbs.logits !== undefined ? f.num(maxAbs.logits, 3) : "—"}
            </td>
            <td>32</td>
            <td>{spec.logits.frac}</td>
            <td>{f.sci(2 ** -spec.logits.frac)}</td>
            <td>±{f.num(2 ** (31 - spec.logits.frac), 3)}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted">{t("s2.actNote")}</p>
      {layers.length > 0 && (
        <details>
          <summary>{t("s2.perLayerTable")}</summary>
          <table className="s0-table">
            <thead>
              <tr>
                <th>{t("s2.col.layer")}</th>
                {ORDER.filter(
                  (k) => k !== "x" && layers[0]![k] !== undefined,
                ).map((k) => (
                  <th key={k}>{t(KIND_KEYS[k]!)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {layers.map((l, i) => (
                <tr key={i}>
                  <td>{i}</td>
                  {ORDER.filter((k) => k !== "x" && l[k] !== undefined).map(
                    (k) => (
                      <td key={k}>{l[k]}</td>
                    ),
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </Section>
  );
}

/** The accumulator of a matrix-vector product: the bound, the chosen width, overflow. */
function Accumulator({ spec }: { spec: Json }) {
  const { t } = useI18n();
  const f = useFormat();
  const acc = spec.accumulator;
  const d = acc.derivation;
  return (
    <Section title={t("s2.accumulator")}>
      <p className="flow-what">{t("s2.accIntro")}</p>
      {d && (
        <p className="s0-formula">
          {t("s2.accFormula", {
            groups: d.groups,
            inputs: f.int(d.inputs),
            group: d.groupSize,
            act: f.int(d.activationMax),
            qmax: d.qmax,
            m: f.int(d.mantissaMax),
            bound: acc.bound.toExponential(3),
            auto: acc.autoBits,
          })}
        </p>
      )}
      <p className={`s0-answer ${acc.canOverflow ? "infeasible" : ""}`}>
        <span>
          {t(acc.canOverflow ? "s2.accNarrow" : "s2.accSafe", {
            bits: acc.bits,
            auto: acc.autoBits ?? acc.bits,
          })}
        </span>
        <small>
          {t(acc.canOverflow ? "s2.accNarrowNote" : "s2.accSafeNote", {
            mode: t(`s2.ovf.${spec.overflow?.mode ?? "saturate"}` as Key),
          })}
        </small>
      </p>
    </Section>
  );
}

/** Integer shift by s with the spec's rounding modes (worked examples). */
function rsh(v: number, s: number, mode: string): number {
  if (mode === "truncate") return Math.floor(v / 2 ** s);
  if (mode === "half-up") return Math.floor((v + 2 ** (s - 1)) / 2 ** s);
  const fl = Math.floor(v / 2 ** s);
  const rest = v - fl * 2 ** s;
  return rest > 2 ** (s - 1) || (rest === 2 ** (s - 1) && fl % 2 !== 0)
    ? fl + 1
    : fl;
}

function wrap(v: number, bits: number): number {
  const m = 2 ** bits;
  return ((((v + m / 2) % m) + m) % m) - m / 2;
}

/** The rounding mode of a spec; specs written before the modes existed say "round-half-up". */
function roundingOf(spec: Json): Scheme["rounding"] {
  const mode = String(spec.rounding?.mode ?? "half-up").replace(/^round-/, "");
  return (ROUNDINGS as string[]).includes(mode)
    ? (mode as Scheme["rounding"])
    : "half-up";
}

function RoundingOverflow({ spec }: { spec: Json }) {
  const { t } = useI18n();
  const mode = roundingOf(spec);
  const ovf = spec.overflow?.mode ?? "saturate";
  const values = [2.5, 3.5, -2.5, 2.25, 2.75];
  const sums: [number, number][] = [
    [32767, 1],
    [30000, 10000],
    [-32768, -1],
  ];
  return (
    <Section title={t("s2.roundOverflow")}>
      <p className="flow-what">{t("s2.roundIntro")}</p>
      <table className="s0-table s2-modes">
        <thead>
          <tr>
            <th>{t("s2.col.exact")}</th>
            {ROUNDINGS.map((r) => (
              <th key={r} className={r === mode ? "current" : ""}>
                {t(`s2.round.${r}` as Key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {values.map((v) => (
            <tr key={v}>
              <td>{v}</td>
              {ROUNDINGS.map((r) => (
                <td key={r} className={r === mode ? "current" : ""}>
                  {rsh(v * 4, 2, r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">
        {t(`s2.roundWhy.${mode}` as Key)} {t("s2.roundWhere")}
      </p>
      <table className="s0-table s2-modes">
        <thead>
          <tr>
            <th>{t("s2.col.sum")}</th>
            {OVERFLOWS.map((o) => (
              <th key={o} className={o === ovf ? "current" : ""}>
                {t(`s2.ovf.${o}` as Key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sums.map(([a, b]) => (
            <tr key={`${a}${b}`}>
              <td>
                {a} + ({b}) = {a + b}
              </td>
              <td className={ovf === "saturate" ? "current" : ""}>
                {Math.min(32767, Math.max(-32768, a + b))}
              </td>
              <td className={ovf === "wrap" ? "current" : ""}>
                {wrap(a + b, 16)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">{t(`s2.ovfWhy.${ovf}` as Key)}</p>
    </Section>
  );
}

/** The lookup tables of the nonlinear functions and how far they are from the exact functions. */
function Tables({ spec }: { spec: Json }) {
  const { t } = useI18n();
  const f = useFormat();
  const errors = useMemo(() => {
    let exp = 0;
    for (let i = 0; i < 512; i++) {
      const tab = Math.round(32768 * Math.exp(-(i + 0.5) / 32)) / 32768;
      for (const d of [i / 32, (i + 1) / 32 - 1e-9])
        exp = Math.max(exp, Math.abs(tab - Math.exp(-d)) / Math.exp(-d));
    }
    let sig = 0;
    for (let i = 0; i < 256; i++) {
      const tab =
        Math.round(32768 / (1 + Math.exp(-(i - 128 + 0.5) / 16))) / 32768;
      for (const z of [(i - 128) / 16, (i - 127) / 16 - 1e-9])
        sig = Math.max(sig, Math.abs(tab - 1 / (1 + Math.exp(-z))));
    }
    let rs = 0;
    for (let tt = 64; tt < 256; tt++) {
      const tab = Math.round(32768 / Math.sqrt((tt + 0.5) / 64)) / 32768;
      for (const x of [tt / 64, (tt + 1) / 64 - 1e-9])
        rs = Math.max(rs, Math.abs(tab - 1 / Math.sqrt(x)) * Math.sqrt(x));
    }
    return { exp, sig, rs, rope: 2 ** -15 };
  }, []);
  const rope = spec.luts?.rope?.entries ?? [];
  const rows: [Key, Key, string, string][] = [
    [
      "s2.lut.rsqrt",
      "s2.lut.rsqrtWhat",
      f.int(spec.luts?.rsqrt?.entries ?? 192),
      f.pct(errors.rs),
    ],
    [
      "s2.lut.exp",
      "s2.lut.expWhat",
      f.int(spec.luts?.exp?.entries ?? 512),
      f.pct(errors.exp),
    ],
    [
      "s2.lut.sigmoid",
      "s2.lut.sigmoidWhat",
      f.int(spec.luts?.sigmoid?.entries ?? 256),
      f.sci(errors.sig),
    ],
    [
      "s2.lut.rope",
      "s2.lut.ropeWhat",
      rope.length === 2 ? `${f.int(rope[0])} × ${f.int(rope[1])}` : "—",
      f.sci(errors.rope),
    ],
  ];
  return (
    <Section title={t("s2.tables")}>
      <p className="flow-what">{t("s2.tablesIntro")}</p>
      <table className="s0-table">
        <thead>
          <tr>
            <th>{t("s2.col.table")}</th>
            <th>{t("s2.col.what")}</th>
            <th>{t("s2.col.entries")}</th>
            <th>{t("s2.col.error")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, what, n, err]) => (
            <tr key={name}>
              <td>{t(name)}</td>
              <td>{t(what)}</td>
              <td>{n}</td>
              <td>{err}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

/** Makes a chip-path scheme the formal numeric spec. */
function Adopt({
  project,
  quantize,
  evaluate,
}: {
  project: string;
  quantize: RunRecord;
  evaluate: RunRecord;
}) {
  const { t } = useI18n();
  const { client } = useKernel();
  const [progress, setProgress] = useState<
    Record<string, { state: StepState; detail?: string }>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await adoptScheme(
        client,
        project,
        quantize,
        evaluate,
        (step, state, detail) =>
          setProgress((p) => ({
            ...p,
            [step]: { state, ...(detail ? { detail } : {}) },
          })),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title={t("s2.adopt")}>
      <p className="flow-what">{t("s2.adoptIntro")}</p>
      <button className="active" disabled={busy} onClick={go}>
        {busy ? t("playground.running") : t("s2.adoptGo")}
      </button>
      <Progress progress={progress} />
      <ErrorLine error={error} />
    </Section>
  );
}

// The readable views of a numeric spec and an accuracy report, also used by the artifact browser.
export {
  Accuracy as NumericAccuracy,
  Weights as NumericWeights,
  Activations as NumericActivations,
  Accumulator as NumericAccumulator,
  RoundingOverflow as NumericRounding,
  Tables as NumericTables,
};
