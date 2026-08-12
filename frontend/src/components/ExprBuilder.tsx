import type {
  Comparison,
  Expr,
  IndicatorInstance,
  IndicatorSpec,
  Op,
} from '../lib/strategy'
import { isComparison, isExprAll, isExprAny, isExprNot } from '../lib/strategy'
import { SelectField, SmallButton, NumberField } from './editor/fields'
import OperandEditor from './editor/OperandEditor'

const MAX_DEPTH = 5

const OPS: { value: Op; label: string }[] = [
  { value: 'gt', label: '>' },
  { value: 'gte', label: '>=' },
  { value: 'lt', label: '<' },
  { value: 'lte', label: '<=' },
  { value: 'eq', label: '==' },
  { value: 'crosses_above', label: 'crosses above' },
  { value: 'crosses_below', label: 'crosses below' },
  { value: 'rising', label: 'rising' },
  { value: 'falling', label: 'falling' },
]

type Kind = 'all' | 'any' | 'not' | 'cmp'

function kindOf(e: Expr): Kind {
  if (isExprAll(e)) return 'all'
  if (isExprAny(e)) return 'any'
  if (isExprNot(e)) return 'not'
  return 'cmp'
}

function defaultComparison(indicators: IndicatorInstance[]): Comparison {
  const firstInd = indicators[0]?.id
  return {
    op: 'gt',
    left: firstInd ? { ind: firstInd } : { price: 'close' },
    right: { value: 0 },
  }
}

function convertKind(expr: Expr, kind: Kind, indicators: IndicatorInstance[]): Expr {
  if (kind === 'cmp') return defaultComparison(indicators)
  if (kind === 'not') {
    const child = isExprAll(expr) || isExprAny(expr) ? expr : isExprNot(expr) ? expr.not : expr
    return { not: child }
  }
  // all / any
  const children: Expr[] = isExprAll(expr)
    ? expr.all
    : isExprAny(expr)
      ? expr.any
      : isExprNot(expr)
        ? [expr.not]
        : [expr]
  return kind === 'all' ? { all: children } : { any: children }
}

interface ExprBuilderProps {
  expr: Expr
  onChange: (e: Expr) => void
  onRemove?: () => void
  indicators: IndicatorInstance[]
  indicatorSpecs: Map<string, IndicatorSpec>
  depth: number
}

export default function ExprBuilder({
  expr,
  onChange,
  onRemove,
  indicators,
  indicatorSpecs,
  depth,
}: ExprBuilderProps) {
  const kind = kindOf(expr)

  const typeSelect = (
    <SelectField
      label="Match"
      value={kind}
      onChange={(v) => onChange(convertKind(expr, v as Kind, indicators))}
      options={[
        { value: 'all', label: 'ALL of' },
        { value: 'any', label: 'ANY of' },
        { value: 'not', label: 'NOT' },
        { value: 'cmp', label: 'Single condition' },
      ]}
    />
  )

  if (kind === 'cmp' && isComparison(expr)) {
    return (
      <div className="flex flex-wrap items-end gap-2 rounded border border-white/10 bg-white/[0.02] p-2">
        {typeSelect}
        <OperandEditor
          label="left"
          value={expr.left}
          onChange={(left) => onChange({ ...expr, left })}
          indicators={indicators}
          indicatorSpecs={indicatorSpecs}
        />
        <SelectField
          label="op"
          value={expr.op}
          onChange={(v) => onChange({ ...expr, op: v as Op })}
          options={OPS}
        />
        {expr.op !== 'rising' && expr.op !== 'falling' && (
          <OperandEditor
            label="right"
            value={expr.right ?? { value: 0 }}
            onChange={(right) => onChange({ ...expr, right })}
            indicators={indicators}
            indicatorSpecs={indicatorSpecs}
          />
        )}
        {(expr.op === 'rising' || expr.op === 'falling') && (
          <NumberField
            label="bars"
            value={expr.bars ?? 1}
            min={1}
            onChange={(v) => onChange({ ...expr, bars: v })}
          />
        )}
        {onRemove && (
          <SmallButton onClick={onRemove} variant="danger">
            Remove
          </SmallButton>
        )}
      </div>
    )
  }

  if (kind === 'not' && isExprNot(expr)) {
    return (
      <div className="rounded border border-white/10 bg-white/[0.02] p-2">
        <div className="mb-2 flex items-center gap-2">
          {typeSelect}
          {onRemove && (
            <SmallButton onClick={onRemove} variant="danger">
              Remove
            </SmallButton>
          )}
        </div>
        <div className="ml-4 border-l border-white/10 pl-3">
          <ExprBuilder
            expr={expr.not}
            onChange={(child) => onChange({ not: child })}
            indicators={indicators}
            indicatorSpecs={indicatorSpecs}
            depth={depth + 1}
          />
        </div>
      </div>
    )
  }

  // all / any
  const children = isExprAll(expr) ? expr.all : isExprAny(expr) ? expr.any : []

  function setChildren(next: Expr[]) {
    onChange(kind === 'all' ? { all: next } : { any: next })
  }

  function updateChild(i: number, child: Expr) {
    const next = children.slice()
    next[i] = child
    setChildren(next)
  }

  function removeChild(i: number) {
    setChildren(children.filter((_, idx) => idx !== i))
  }

  function addComparison() {
    setChildren([...children, defaultComparison(indicators)])
  }

  function addGroup() {
    setChildren([...children, { all: [defaultComparison(indicators)] }])
  }

  return (
    <div className="rounded border border-white/10 bg-white/[0.02] p-2">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {typeSelect}
        <SmallButton onClick={addComparison}>+ condition</SmallButton>
        {depth < MAX_DEPTH - 1 && <SmallButton onClick={addGroup}>+ group</SmallButton>}
        {onRemove && (
          <SmallButton onClick={onRemove} variant="danger">
            Remove
          </SmallButton>
        )}
      </div>
      <div className="ml-4 flex flex-col gap-2 border-l border-white/10 pl-3">
        {children.length === 0 && (
          <p className="text-xs text-gray-600">No conditions yet — add one above.</p>
        )}
        {children.map((child, i) => (
          <ExprBuilder
            key={i}
            expr={child}
            onChange={(c) => updateChild(i, c)}
            onRemove={() => removeChild(i)}
            indicators={indicators}
            indicatorSpecs={indicatorSpecs}
            depth={depth + 1}
          />
        ))}
      </div>
    </div>
  )
}
