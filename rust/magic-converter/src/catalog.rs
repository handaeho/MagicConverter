//! Ordered declarations from the pinned catalog and an independent route search.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::borrow::Cow;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::path::Path;

#[derive(Deserialize, Serialize)]
pub struct Format {
    pub id: String,
    label: String,
    pub category: String,
    pub extensions: Vec<String>,
    readable: bool,
    pub writable: bool,
}

#[derive(Deserialize, Serialize)]
pub struct Action {
    pub key: String,
    pub label: String,
    description: String,
    suffix: String,
}

#[derive(Deserialize)]
struct Policy {
    target: Option<String>,
    sources: Option<Vec<String>>,
    intent: String,
    batch: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Edge {
    pub source: String,
    pub target: String,
    pub engine_id: String,
    pub action_key: String,
    cost: u32,
    intent: String,
    direct_only: bool,
    terminal_only: bool,
    office_queue: bool,
}

#[derive(Deserialize)]
struct Engine {
    edges: Vec<Edge>,
}

#[derive(Deserialize)]
pub struct Catalog {
    pub baseline: String,
    pub formats: Vec<Format>,
    pub actions: Vec<Action>,
    policies: HashMap<String, Policy>,
    converters: Vec<Engine>,
    unavailable_reasons: HashMap<String, String>,
    pub default_preferences: Value,
    #[serde(skip)]
    extensions: HashMap<String, (usize, usize)>,
    #[serde(skip)]
    adjacency: HashMap<(String, String), Vec<(usize, usize)>>,
}

#[derive(Debug, Serialize)]
pub struct Route<'a> {
    pub steps: Vec<&'a Edge>,
    cost: u32,
}

#[derive(Serialize)]
pub struct Assessment<'a> {
    pub route: Option<Route<'a>>,
    pub reason: String,
}

impl Catalog {
    pub fn load() -> Result<Self, serde_json::Error> {
        let mut catalog: Self = serde_json::from_str(include_str!("../catalog.json"))?;
        for (format_index, format) in catalog.formats.iter().enumerate() {
            for (extension_index, extension) in format.extensions.iter().enumerate() {
                catalog
                    .extensions
                    .insert(extension.clone(), (format_index, extension_index));
            }
        }
        for (engine_index, engine) in catalog.converters.iter().enumerate() {
            for (edge_index, edge) in engine.edges.iter().enumerate() {
                catalog
                    .adjacency
                    .entry((edge.intent.clone(), edge.source.clone()))
                    .or_default()
                    .push((engine_index, edge_index));
            }
        }
        Ok(catalog)
    }

    pub fn detect(&self, name: &Path) -> Option<(&Format, &str)> {
        let name = name.file_name()?.to_str()?;
        let name = if name.bytes().any(|byte| byte.is_ascii_uppercase()) {
            Cow::Owned(name.to_ascii_lowercase())
        } else {
            Cow::Borrowed(name)
        };
        name.match_indices('.').find_map(|(dot, _)| {
            let &(format, extension) = self.extensions.get(&name[dot + 1..])?;
            let format = &self.formats[format];
            Some((format, format.extensions[extension].as_str()))
        })
    }

    pub fn target_action(
        &self,
        target: &str,
        paths: &[std::path::PathBuf],
        disabled: &[String],
    ) -> Option<&Action> {
        let target = target.trim_start_matches('.').to_ascii_lowercase();
        let format = format!("file.{target}");
        let primary = self
            .detect(Path::new(&format))
            .map_or(target.as_str(), |(format, _)| format.extensions[0].as_str());
        let suffix = format!(".{primary}");
        self.actions.iter().find(|action| {
            action.suffix == suffix
                && paths
                    .iter()
                    .all(|path| self.assess(path, &action.key, disabled).route.is_some())
        })
    }

    pub fn assess(&self, source: &Path, action: &str, disabled: &[String]) -> Assessment<'_> {
        let fail = |reason: &str| Assessment {
            route: None,
            reason: reason.to_owned(),
        };
        let success = |route| Assessment {
            route: Some(route),
            reason: String::new(),
        };
        let Some(policy) = self.policies.get(action) else {
            return fail("알 수 없는 작업입니다");
        };
        if policy.batch {
            return success(Route {
                steps: Vec::new(),
                cost: 0,
            });
        }
        let Some((format, _)) = self.detect(source).filter(|(format, _)| format.readable) else {
            return fail("지원하지 않는 입력 형식입니다");
        };
        if policy
            .sources
            .as_ref()
            .is_some_and(|sources| !sources.contains(&format.id))
        {
            return fail("선택한 모든 파일에 적용할 수 없습니다");
        }
        let target = policy.target.as_deref().unwrap_or(&format.id);
        if let Some(route) = self.plan(&format.id, target, &policy.intent, disabled) {
            return success(route);
        }
        if !disabled.is_empty()
            && let Some(route) = self.plan(&format.id, target, &policy.intent, &[])
        {
            let mut reasons = Vec::new();
            for step in route.steps {
                if disabled.contains(&step.engine_id)
                    && let Some(reason) = self.unavailable_reasons.get(&step.engine_id)
                    && !reasons.contains(&reason)
                {
                    reasons.push(reason);
                }
            }
            return Assessment {
                route: None,
                reason: reasons
                    .into_iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .join(" / "),
            };
        }
        fail("지원하는 변환 경로가 없습니다")
    }

    fn plan(
        &self,
        source: &str,
        target: &str,
        intent: &str,
        disabled: &[String],
    ) -> Option<Route<'_>> {
        const MAX_STEPS: usize = 3;
        let edge_at = |(engine, edge): (usize, usize)| &self.converters[engine].edges[edge];
        let mut serial = 0;
        let mut queue = BinaryHeap::from([Reverse((0, 0, serial, source, Vec::new()))]);
        let mut distances = HashMap::from([((source, 0), 0)]);
        let mut adjacency_key = (intent.to_owned(), String::new());
        while let Some(Reverse((cost, hops, _, node, steps))) = queue.pop() {
            if distances.get(&(node, hops)) != Some(&cost) {
                continue;
            }
            if hops > 0 && node == target {
                return Some(Route {
                    steps: steps.into_iter().map(edge_at).collect(),
                    cost,
                });
            }
            if hops == MAX_STEPS {
                continue;
            }
            adjacency_key.1.clear();
            adjacency_key.1.push_str(node);
            for &index in self.adjacency.get(&adjacency_key).into_iter().flatten() {
                let edge = edge_at(index);
                if disabled.contains(&edge.engine_id)
                    || (source == target && (hops != 0 || edge.target != target))
                    || (edge.source == edge.target && (hops != 0 || source != target))
                    || (edge.direct_only && (hops != 0 || edge.target != target))
                    || (edge.terminal_only && edge.target != target)
                    || (edge.target != target
                        && (edge.target == source
                            || steps
                                .iter()
                                .any(|&step| edge_at(step).target == edge.target)))
                {
                    continue;
                }
                let state = (edge.target.as_str(), hops + 1);
                let candidate = cost + edge.cost;
                if distances
                    .get(&state)
                    .is_some_and(|previous| candidate >= *previous)
                {
                    continue;
                }
                distances.insert(state, candidate);
                serial += 1;
                let mut next_steps = steps.clone();
                next_steps.push(index);
                queue.push(Reverse((candidate, hops + 1, serial, state.0, next_steps)));
            }
        }
        None
    }
}
