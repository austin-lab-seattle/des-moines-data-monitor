import { lazy, Suspense } from 'react';
import Dashboard from './Dashboard';

const TeamConsole = lazy(() => import('./TeamConsole'));

function App() {
  return window.location.pathname.startsWith('/team')
    ? <Suspense fallback={<div className="loading-screen">Loading team workspace…</div>}><TeamConsole /></Suspense>
    : <Dashboard />;
}

export default App;
