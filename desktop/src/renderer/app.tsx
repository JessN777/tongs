import {
  Component,
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createRoot } from "react-dom/client";
import type { RepositoryDto } from "../shared/bridge.js";
import {
  FeatureRegistry,
  InlineAnchorStore,
  Navigator,
  reconcileDiscoveryRoute,
  type AppRoute,
  type FeatureContext,
} from "./core/navigation.js";
import { ColorSchemeToggle } from "./core/appearance.js";
import { safeError } from "./core/presentation.js";
import { QueryCoordinator } from "./core/query.js";
import {
  ServiceNotices,
  ServiceStatusLine,
  ServiceStatusModel,
  isResyncEvent,
} from "./core/service-status.js";
import { createDiffFeature } from "./features/diff/index.js";
import { createInboxFeature } from "./features/inbox/index.js";
import {
  createPluginsFeature,
  usePluginSnapshot,
} from "./features/plugins/index.js";
import { PluginLocationPublisher } from "./features/plugins/runtime.js";
import { createPipelinesFeature } from "./features/pipelines/index.js";
import { createReviewFeature } from "./features/review/index.js";
import { RepositoryNavigation } from "./features/repositories/index.js";
import {
  WorkspaceUtilityController,
  WorkspaceUtilityOverlay,
  createUtilitiesFeature,
} from "./features/utilities/index.js";
import {
  createCommitsFeature,
  createReviewOverviewFeature,
} from "./features/review-detail/index.js";

const bridge = window.tongs;
const navigator = new Navigator();
const queries = new QueryCoordinator(bridge);
const shellQueries = new QueryCoordinator(bridge);
const registry = new FeatureRegistry();
const pluginsFeature = createPluginsFeature(bridge);
const pluginLocations = new PluginLocationPublisher(bridge);
const utilityController = new WorkspaceUtilityController();
const serviceStatus = new ServiceStatusModel();
const inlineAnchors = new InlineAnchorStore();
registry.register(createInboxFeature());
registry.register(createReviewOverviewFeature());
registry.register(createCommitsFeature());
registry.register(createDiffFeature());
registry.register(pluginsFeature);
registry.register(createPipelinesFeature());
registry.register(createReviewFeature(bridge));
registry.register(createUtilitiesFeature(utilityController));

function App(): ReactNode {
  const [route, setRoute] = useState<AppRoute>(navigator.route);
  const [repositories, setRepositories] = useState<readonly RepositoryDto[]>(
    [],
  );
  const [repositoriesReady, setRepositoriesReady] = useState(false);
  const [repositoriesError, setRepositoriesError] = useState<string | null>(
    null,
  );
  const discoveryRetry = useRef<(() => void) | null>(null);
  const [repositoryGeneration, setRepositoryGeneration] = useState(0);
  const [routeNotice, setRouteNotice] = useState<string | null>(null);
  const plugins = usePluginSnapshot(pluginsFeature.runtime);
  useEffect(() => navigator.subscribe(setRoute), []);
  useEffect(
    () =>
      bridge.onEvent((event) => {
        if (isResyncEvent(event.name, event.data))
          serviceStatus.updatesAvailable();
      }),
    [],
  );
  // Opening another view reads it afresh, so the updates warning has done its
  // job once the route changes.
  useEffect(() => {
    serviceStatus.clearUpdates();
  }, [route]);
  useEffect(() => {
    pluginsFeature.runtime.start();
    return () => {
      void pluginsFeature.runtime.dispose();
      void pluginLocations.dispose();
    };
  }, []);
  useEffect(() => {
    void pluginLocations.publish(route);
  }, [route]);
  useEffect(
    () => () => {
      void queries.cancelAll();
      void shellQueries.cancelAll();
    },
    [],
  );
  useEffect(() => {
    void publishNativeProbe(serviceStatus);
  }, []);
  const navigation = useCallback((next: AppRoute) => {
    setRouteNotice(null);
    inlineAnchors.retainFor(next);
    navigator.navigate(next);
  }, []);
  const onDiscovery = useCallback(
    (next: readonly RepositoryDto[]) => {
      const reconciliation = reconcileDiscoveryRoute(navigator.route, next);
      setRepositories(next);
      setRepositoriesReady(true);
      setRepositoryGeneration((current) => current + 1);
      if (reconciliation.removed) {
        navigation(reconciliation.route);
        setRouteNotice(
          "The selected repository is no longer in the local workspace. Showing All reviews. Unsaved review text and saved drafts were preserved.",
        );
      } else if (reconciliation.route !== navigator.route) {
        navigation(reconciliation.route);
      } else {
        setRouteNotice(null);
      }
    },
    [navigation],
  );
  const onDiscoveryError = useCallback(
    (message: string | null, retry: () => void) => {
      discoveryRetry.current = retry;
      setRepositoriesError(message);
    },
    [],
  );
  const retryDiscovery = useCallback(() => discoveryRetry.current?.(), []);
  const feature = useMemo(() => registry.find(route), [route]);
  const selected = route.kind === "inbox" ? route.repository : undefined;
  const featureContext: FeatureContext = {
    bridge,
    queries,
    repositories,
    repositoriesReady,
    repositoriesError,
    retryDiscovery,
    repositoryGeneration,
    reviewPanels: registry.reviewPanels(),
    inlineAnchors,
    navigate: navigation,
  };
  const commands = [
    ...registry.commands(featureContext, route),
    ...pluginsFeature.commandsFor(plugins),
  ].sort(
    (left, right) =>
      left.order - right.order || left.id.localeCompare(right.id),
  );
  return (
    <>
      <header className="app-bar">
        <div className="brand">
          <img src="/icon.png" alt="" width="32" height="32" />
          <strong>Tongs</strong>
        </div>
        <div className="app-actions">
          {commands.map((command) => {
            const disabledReason = command.disabledReason(
              featureContext,
              route,
            );
            return (
              <button
                key={command.id}
                className="button button-secondary"
                disabled={disabledReason !== null}
                title={disabledReason ?? undefined}
                onClick={() => void command.run(featureContext, route)}
              >
                {command.label}
              </button>
            );
          })}
          <ColorSchemeToggle bridge={bridge} />
          <ServiceStatusLine model={serviceStatus} bridge={bridge} />
        </div>
      </header>
      <ServiceNotices model={serviceStatus} />
      <div className="app-layout">
        <RepositoryNavigation
          bridge={bridge}
          queries={shellQueries}
          selected={selected}
          navigate={navigation}
          onDiscovery={onDiscovery}
          onDiscoveryError={onDiscoveryError}
          pluginNavigation={pluginsFeature.navigation(
            plugins,
            route,
            navigation,
          )}
        />
        <main id="content" className="content" tabIndex={-1}>
          {routeNotice && (
            <div className="notice notice-warning" role="status">
              {routeNotice}
            </div>
          )}
          <ErrorBoundary
            key={`${route.kind}:${
              route.kind === "review"
                ? `${route.item.handle}:${route.panel}`
                : route.kind === "plugin"
                  ? `${route.pluginId}:${route.navigationId}:${route.moduleId}`
                  : (route.repository?.handle ?? "all")
            }`}
          >
            {feature.render(featureContext, route)}
          </ErrorBoundary>
        </main>
      </div>
      <WorkspaceUtilityOverlay
        controller={utilityController}
        context={featureContext}
      />
    </>
  );
}

/**
 * Keeps one view's failure inside the content pane. Try again mounts the view
 * afresh in place; logging carries only the safe error text, never the raw
 * error or its component stack.
 */
export class ErrorBoundary extends Component<
  { readonly children: ReactNode },
  { readonly failed: boolean; readonly attempt: number }
> {
  state = { failed: false, attempt: 0 };
  static getDerivedStateFromError(): { readonly failed: boolean } {
    return { failed: true };
  }
  componentDidCatch(error: Error): void {
    console.error(safeError(error));
  }
  #retry = (): void => {
    this.setState((current) => ({
      failed: false,
      attempt: current.attempt + 1,
    }));
  };
  render(): ReactNode {
    return this.state.failed ? (
      <>
        <h1 className="view-title">This view could not be displayed</h1>
        <div className="notice notice-error" role="alert">
          The local view encountered an error. Try again, or choose another
          repository or review to continue.
        </div>
        <button className="button button-secondary" onClick={this.#retry}>
          Try again
        </button>
      </>
    ) : (
      <Fragment key={this.state.attempt}>{this.props.children}</Fragment>
    );
  }
}

async function publishNativeProbe(status: ServiceStatusModel): Promise<void> {
  try {
    const assets = await bridge.listAssets().result;
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl");
    const debug = gl?.getExtension("WEBGL_debug_renderer_info");
    const probe = {
      assets: assets.length,
      processGlobal: typeof globalThis.process,
      requireGlobal: typeof globalThis.require,
      webglVendor:
        gl && debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : null,
      webglRenderer:
        gl && debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : null,
    };
    status.probeSucceeded();
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
    document.documentElement.dataset.tongsProbe = JSON.stringify(probe);
  } catch {
    status.probeFailed();
  }
}

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("Missing React application root");
createRoot(root).render(<App />);
