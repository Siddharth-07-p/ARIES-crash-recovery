/**
 * Regression Test Suite for Synthetic Workload Summary display and StatusBadge.
 * Verifies loading, empty, and populated states with confirmed Phase 16D response shapes.
 */

const assert = require('node:assert');
const test = require('node:test');
const path = require('node:path');
const esbuild = require('esbuild');

const frontendDir = path.resolve(__dirname, '..');

// Helper to compile TSX on the fly and execute with React SSR
function compileAndRun(tsxSnippet) {
  const result = esbuild.buildSync({
    stdin: {
      contents: tsxSnippet,
      resolveDir: frontendDir,
      loader: 'tsx',
    },
    bundle: true,
    write: false,
    format: 'cjs',
    platform: 'node',
  });
  return eval(result.outputFiles[0].text);
}

test('WorkloadSection renders populated metrics and badge-available for confirmed Phase 16D response', () => {
  const code = `
    const React = require('react');
    const ReactDOMServer = require('react-dom/server');
    const WorkloadSection = require('./src/components/WorkloadSection').default;

    const confirmedResponse = {
      status: 'available',
      planned_operations: 70,
      executed_operations: 70,
      transactions_started: 10,
      transactions_committed: 8,
      transactions_aborted: 2,
      transactions_incomplete: 0,
      reads_executed: 17,
      updates_executed: 33,
      is_valid: true,
      crash_configured: false,
      crash_observed: false,
      fault_point: null,
    };

    const html = ReactDOMServer.renderToString(
      React.createElement(WorkloadSection, {
        workload: {
          data: confirmedResponse,
          loading: false,
          error: null,
          lastUpdated: new Date(),
        },
      })
    );

    module.exports = { html };
  `;

  const { html } = compileAndRun(code);

  assert.ok(!html.includes('No Synthetic Workload Recorded'), 'Must not display empty state when workload is available');
  assert.ok(!html.includes('Loading Synthetic Workload...'), 'Must not display loading state when data is resolved');
  assert.ok(html.includes('Executed Operations'), 'Must display Executed Operations card');
  assert.ok(html.includes('Transactions Started'), 'Must display Transactions Started card');
  assert.ok(html.includes('Committed'), 'Must display Committed card');
  assert.ok(html.includes('Aborted'), 'Must display Aborted card');
  assert.ok(html.includes('State Validation Passed'), 'Must display State Validation Passed');
  assert.ok(html.includes('badge-available'), 'Must render badge-available variant for available status');
  assert.ok(!html.includes('badge-uninitialized'), 'Must NOT fall back to badge-uninitialized');
});

test('WorkloadSection renders loading state panel when loading=true and data=null', () => {
  const code = `
    const React = require('react');
    const ReactDOMServer = require('react-dom/server');
    const WorkloadSection = require('./src/components/WorkloadSection').default;

    const html = ReactDOMServer.renderToString(
      React.createElement(WorkloadSection, {
        workload: {
          data: null,
          loading: true,
          error: null,
          lastUpdated: null,
        },
      })
    );

    module.exports = { html };
  `;

  const { html } = compileAndRun(code);

  assert.ok(html.includes('Loading Synthetic Workload...'), 'Must display loading state header');
  assert.ok(html.includes('Retrieving latest synthetic transaction workload'), 'Must display loading description');
  assert.ok(!html.includes('No Synthetic Workload Recorded'), 'Must not prematurely display No Workload Recorded during initial fetch');
  assert.ok(html.includes('badge-checking'), 'Must render checking badge during initial loading');
});

test('WorkloadSection renders empty state when status is not_available and loading=false', () => {
  const code = `
    const React = require('react');
    const ReactDOMServer = require('react-dom/server');
    const WorkloadSection = require('./src/components/WorkloadSection').default;

    const html = ReactDOMServer.renderToString(
      React.createElement(WorkloadSection, {
        workload: {
          data: { status: 'not_available' },
          loading: false,
          error: null,
          lastUpdated: new Date(),
        },
      })
    );

    module.exports = { html };
  `;

  const { html } = compileAndRun(code);

  assert.ok(html.includes('No Synthetic Workload Recorded'), 'Must display empty state when not_available');
  assert.ok(html.includes('badge-not_run'), 'Must render badge-not_run variant');
});

test('WorkloadSection resiliently detects availability when operations are present without explicit status=available', () => {
  const code = `
    const React = require('react');
    const ReactDOMServer = require('react-dom/server');
    const WorkloadSection = require('./src/components/WorkloadSection').default;

    const html = ReactDOMServer.renderToString(
      React.createElement(WorkloadSection, {
        workload: {
          data: {
            status: null,
            executed_operations: 70,
            planned_operations: 70,
            transactions_started: 10,
            transactions_committed: 8,
            transactions_aborted: 2,
            is_valid: true,
          },
          loading: false,
          error: null,
          lastUpdated: new Date(),
        },
      })
    );

    module.exports = { html };
  `;

  const { html } = compileAndRun(code);

  assert.ok(!html.includes('No Synthetic Workload Recorded'), 'Must recognize executed operations as available');
  assert.ok(html.includes('Executed Operations'), 'Must render metrics grid');
});

test('StatusBadge maps available status to badge-available class', () => {
  const code = `
    const React = require('react');
    const ReactDOMServer = require('react-dom/server');
    const StatusBadge = require('./src/components/StatusBadge').default;

    const htmlAvailable = ReactDOMServer.renderToString(
      React.createElement(StatusBadge, { status: 'available' })
    );

    const htmlNotAvailable = ReactDOMServer.renderToString(
      React.createElement(StatusBadge, { status: 'not_available' })
    );

    const htmlUninitialized = ReactDOMServer.renderToString(
      React.createElement(StatusBadge, { status: 'uninitialized' })
    );

    module.exports = { htmlAvailable, htmlNotAvailable, htmlUninitialized };
  `;

  const { htmlAvailable, htmlNotAvailable, htmlUninitialized } = compileAndRun(code);

  assert.ok(htmlAvailable.includes('badge-available'), 'status="available" must yield badge-available');
  assert.ok(!htmlAvailable.includes('badge-uninitialized'), 'status="available" must not fall back to badge-uninitialized');
  assert.ok(htmlNotAvailable.includes('badge-not_run'), 'status="not_available" must yield badge-not_run');
  assert.ok(htmlUninitialized.includes('badge-uninitialized'), 'status="uninitialized" must yield badge-uninitialized');
});
