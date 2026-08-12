import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import Settings from './pages/Settings'
import Charts from './pages/Charts'
import Chains from './pages/Chains'
import Data from './pages/Data'
import Strategies from './pages/Strategies'
import StrategyNew from './pages/StrategyNew'
import StrategyEditor from './pages/StrategyEditor'
import PromoteToLive from './pages/PromoteToLive'
import Backtests from './pages/Backtests'
import BacktestResult from './pages/BacktestResult'
import BacktestCompare from './pages/BacktestCompare'
import SweepDetail from './pages/SweepDetail'
import Running from './pages/Running'
import RunDetail from './pages/RunDetail'
import LiveProbe from './pages/LiveProbe'
import Docs from './pages/Docs'
import Orders from './pages/Orders'
import Optimize from './pages/Optimize'
import OptimizeStudy from './pages/OptimizeStudy'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/strategies" element={<Strategies />} />
        <Route path="/strategies/new" element={<StrategyNew />} />
        <Route path="/strategies/new/blank" element={<StrategyEditor />} />
        <Route path="/strategies/new/editor" element={<StrategyEditor />} />
        <Route path="/strategies/:slug" element={<StrategyEditor />} />
        {/* Not linked from Sidebar on purpose — arms a live run against a
            real Schwab account (docs/plan/07-paper-trading.md). Reachable
            via a button in StrategyEditor, same "direct navigation only"
            precedent as /live-probe below. */}
        <Route path="/strategies/:slug/promote" element={<PromoteToLive />} />
        <Route path="/running" element={<Running />} />
        <Route path="/running/:runId" element={<RunDetail />} />
        <Route path="/backtests" element={<Backtests />} />
        <Route path="/backtests/compare" element={<BacktestCompare />} />
        <Route path="/backtests/sweep/:sweepId" element={<SweepDetail />} />
        <Route path="/backtests/:id" element={<BacktestResult />} />
        <Route path="/optimize" element={<Optimize />} />
        <Route path="/optimize/:studyId" element={<OptimizeStudy />} />
        <Route path="/charts" element={<Charts />} />
        <Route path="/chains" element={<Chains />} />
        <Route path="/orders" element={<Orders />} />
        <Route path="/data" element={<Data />} />
        <Route path="/docs" element={<Docs />} />
        <Route path="/settings" element={<Settings />} />
        {/* Not linked from Sidebar on purpose — Phase 5.5 hidden dev screen,
            places REAL orders. Reachable only by navigating here directly. */}
        <Route path="/live-probe" element={<LiveProbe />} />
      </Route>
    </Routes>
  )
}
