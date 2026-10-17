"""k-Nearest Neighbors (kNN) Deck Similarity Engine using TF-IDF and Recency Decay."""

import logging
import warnings
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from django.conf import settings
from mtg_archetypes import slugify_archetype
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfTransformer

from core.models.card import normalize_card_name
from core.models.deck import Deck


logger = logging.getLogger(__name__)

_GLOBAL_INDEX: "DeckKNNIndex | None" = None
_GLOBAL_INDEX_LOADED: bool = False


class DeckKNNIndex:
    """In-memory TF-IDF index for fast nearest-deck search with 1-year exponential decay."""

    def __init__(
        self,
        format_filter: str | None = None,
        half_life_days: float = 365.0,
        lazy: bool = False,
    ):
        self.format_filter = format_filter
        self.half_life_days = half_life_days

        self.deck_ids: list[str] | np.ndarray = []
        self.deck_dates: list[Any] | np.ndarray = []
        self.deck_players: list[str] | np.ndarray = []
        self.deck_archetypes: list[str] | np.ndarray = []
        self.deck_formats: list[str] | np.ndarray = []
        self.deck_colors: list[str] | np.ndarray = []
        self.deck_color_names: list[str] | np.ndarray = []
        self.deck_classification_methods: list[str] | np.ndarray = []

        self.vocab: dict[str, int] = {}
        self.inv_vocab: list[str] | np.ndarray = []
        self.tfidf_matrix: csr_matrix | None = None
        self.transformer: TfidfTransformer | None = None
        self._format_submatrices: dict[str, tuple[csr_matrix, np.ndarray]] = {}

        if not lazy:
            self._build_index()

    def _build_index(self) -> None:
        """Build TF-IDF sparse matrix from database decks."""
        qs = Deck.objects.select_related("tournament").all()
        if self.format_filter:
            qs = qs.filter(format=self.format_filter)

        decks_data = list(
            qs.values(
                "id",
                "format",
                "player",
                "archetype",
                "classification_method",
                "is_auto_classified",
                "colors",
                "color_name",
                "tournament__date",
                "mainboard",
                "sideboard",
            )
        )

        if not decks_data:
            return

        vocab_map: dict[str, int] = {}
        rows, cols, data = [], [], []

        for row_idx, d in enumerate(decks_data):
            self.deck_ids.append(d["id"])
            self.deck_formats.append(d["format"])
            self.deck_players.append(d["player"])
            self.deck_archetypes.append(d["archetype"])
            self.deck_colors.append(d.get("colors", "") or "")
            self.deck_color_names.append(d.get("color_name", "") or "")
            method = d.get("classification_method") or (
                "fallback" if d.get("is_auto_classified") else ""
            )
            self.deck_classification_methods.append(method)
            self.deck_dates.append(d["tournament__date"])

            # Count cards: mainboard 1.0, sideboard 0.5
            counts: dict[str, float] = {}
            for item in d["mainboard"]:
                c = normalize_card_name(item.get("card", ""))
                if c:
                    counts[c] = counts.get(c, 0.0) + float(item.get("count", 1))

            for item in d["sideboard"]:
                c = normalize_card_name(item.get("card", ""))
                if c:
                    counts[c] = counts.get(c, 0.0) + 0.5 * float(item.get("count", 1))

            for card, cnt in counts.items():
                if card not in vocab_map:
                    vocab_map[card] = len(vocab_map)
                col_idx = vocab_map[card]
                rows.append(row_idx)
                cols.append(col_idx)
                data.append(cnt)

        self.vocab = vocab_map
        self.inv_vocab = [None] * len(vocab_map)  # type: ignore
        for card, idx in vocab_map.items():
            self.inv_vocab[idx] = card

        n_decks = len(decks_data)
        n_features = len(vocab_map)
        term_matrix = csr_matrix(
            (data, (rows, cols)), shape=(n_decks, n_features), dtype=np.float32
        )

        self.transformer = TfidfTransformer(norm="l2", sublinear_tf=True)
        self.tfidf_matrix = self.transformer.fit_transform(term_matrix)

        self.deck_ids = np.array(self.deck_ids)
        self.deck_formats = np.array(self.deck_formats)
        self.deck_players = np.array(self.deck_players)
        self.deck_archetypes = np.array(self.deck_archetypes)
        self.deck_colors = np.array(self.deck_colors)
        self.deck_color_names = np.array(self.deck_color_names)
        self.deck_classification_methods = np.array(self.deck_classification_methods)
        self.deck_dates = np.array(self.deck_dates)
        self._deck_dates_d = self._compute_dates_d(self.deck_dates)

    @staticmethod
    def _compute_dates_d(raw_dates: Any) -> np.ndarray:
        if hasattr(raw_dates, "astype"):
            try:
                return raw_dates.astype("datetime64[D]")
            except (ValueError, TypeError) as exc:
                logger.debug("Failed to cast dates directly to datetime64[D]: %s", exc)
        return np.array(
            [
                d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]
                for d in raw_dates
            ],
            dtype="datetime64[D]",
        )

    def _ensure_arrays(self) -> None:
        if not isinstance(self.deck_ids, np.ndarray):
            self.deck_ids = np.array(self.deck_ids)
        if not isinstance(self.deck_formats, np.ndarray):
            self.deck_formats = np.array(self.deck_formats)
        if not isinstance(self.deck_players, np.ndarray):
            self.deck_players = np.array(self.deck_players)
        if not isinstance(self.deck_archetypes, np.ndarray):
            self.deck_archetypes = np.array(self.deck_archetypes)
        if not isinstance(self.deck_colors, np.ndarray):
            self.deck_colors = np.array(self.deck_colors)
        if not isinstance(self.deck_color_names, np.ndarray):
            self.deck_color_names = np.array(self.deck_color_names)
        if not isinstance(self.deck_dates, np.ndarray):
            self.deck_dates = np.array(self.deck_dates)
        if not isinstance(self.deck_classification_methods, np.ndarray):
            self.deck_classification_methods = np.array(
                self.deck_classification_methods
            )

    def _get_dates_d(self) -> np.ndarray:
        if getattr(self, "_deck_dates_d", None) is None:
            self._deck_dates_d = self._compute_dates_d(self.deck_dates)
        return self._deck_dates_d

    def save(self, file_path: Path | str) -> Path:
        """Save the fitted index to a native NumPy/SciPy compressed .npz archive."""
        if self.tfidf_matrix is None or self.transformer is None:
            raise ValueError("Cannot save an unbuilt or empty index.")

        dest = Path(file_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            dest,
            data=self.tfidf_matrix.data,
            indices=self.tfidf_matrix.indices,
            indptr=self.tfidf_matrix.indptr,
            shape=self.tfidf_matrix.shape,
            idf=self.transformer.idf_,
            deck_ids=np.array(self.deck_ids),
            deck_formats=np.array(self.deck_formats),
            deck_players=np.array(self.deck_players),
            deck_archetypes=np.array(self.deck_archetypes),
            deck_colors=np.array(self.deck_colors),
            deck_color_names=np.array(self.deck_color_names),
            deck_classification_methods=np.array(self.deck_classification_methods),
            deck_dates=np.array(
                [
                    d.isoformat() if hasattr(d, "isoformat") else str(d)
                    for d in self.deck_dates
                ]
            ),
            vocab=np.array(self.inv_vocab),
        )
        return dest

    @classmethod
    def load(
        cls, file_path: Path | str, half_life_days: float = 365.0
    ) -> "DeckKNNIndex":
        """Load a fitted index from a native NumPy/SciPy compressed .npz archive."""
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"kNN index file not found at: {src}")

        instance = cls(format_filter=None, half_life_days=half_life_days, lazy=True)
        with np.load(src, allow_pickle=False) as f:
            instance.tfidf_matrix = csr_matrix(
                (f["data"], f["indices"], f["indptr"]), shape=f["shape"]
            )
            instance.deck_ids = f["deck_ids"]
            instance.deck_formats = f["deck_formats"]
            instance.deck_players = f["deck_players"]
            instance.deck_archetypes = f["deck_archetypes"]
            instance.deck_colors = (
                f["deck_colors"]
                if "deck_colors" in f
                else np.array([""] * len(instance.deck_ids))
            )
            instance.deck_color_names = (
                f["deck_color_names"]
                if "deck_color_names" in f
                else np.array([""] * len(instance.deck_ids))
            )
            instance.deck_classification_methods = (
                f["deck_classification_methods"]
                if "deck_classification_methods" in f
                else np.array(["rule"] * len(instance.deck_ids))
            )
            instance.deck_dates = f["deck_dates"]
            instance._deck_dates_d = instance._compute_dates_d(instance.deck_dates)
            instance.inv_vocab = f["vocab"]
            instance.vocab = {card: idx for idx, card in enumerate(instance.inv_vocab)}

            idf = f["idf"]
            transformer = TfidfTransformer(norm="l2", sublinear_tf=True)
            transformer.idf_ = idf
            instance.transformer = transformer

        return instance

    def vector_from_decklist(
        self, mainboard: list[dict[str, Any]], sideboard: list[dict[str, Any]]
    ) -> csr_matrix:
        """Convert an arbitrary decklist to a normalized TF-IDF vector in this index's space."""
        if not self.vocab or self.transformer is None:
            return csr_matrix((1, max(1, len(self.vocab))), dtype=np.float32)

        cols, data = [], []
        counts: dict[str, float] = {}
        for item in mainboard:
            c = normalize_card_name(item.get("card", ""))
            if c:
                counts[c] = counts.get(c, 0.0) + float(item.get("count", 1))

        for item in sideboard:
            c = normalize_card_name(item.get("card", ""))
            if c:
                counts[c] = counts.get(c, 0.0) + 0.5 * float(item.get("count", 1))

        for card, cnt in counts.items():
            if card in self.vocab:
                cols.append(self.vocab[card])
                data.append(cnt)

        rows = [0] * len(cols)
        raw_vec = csr_matrix(
            (data, (rows, cols)), shape=(1, len(self.vocab)), dtype=np.float32
        )
        return self.transformer.transform(raw_vec)

    def query(
        self,
        query_vec: csr_matrix,
        reference_date: date | None = None,
        top_k: int = 5,
        exclude_deck_id: str | None = None,
        format_filter: str | None = None,
        exclude_format: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find the top_k closest decks using cosine similarity and 1-year recency decay."""
        if self.tfidf_matrix is None or self.tfidf_matrix.shape[0] == 0:
            return []

        self._ensure_arrays()

        # Cosine similarity is dot product because rows are L2-normalized
        similarities = (self.tfidf_matrix @ query_vec.T).toarray().ravel()

        mask = similarities > 0.01
        if format_filter:
            mask &= self.deck_formats == format_filter
        if exclude_format:
            mask &= self.deck_formats != exclude_format
        if exclude_deck_id:
            mask &= self.deck_ids != exclude_deck_id

        candidate_indices = np.where(mask)[0]
        if len(candidate_indices) == 0:
            return []

        cand_sims = similarities[candidate_indices]
        ref_d = reference_date or date.today()
        ref_np = np.datetime64(ref_d, "D")

        cand_dates = self._get_dates_d()[candidate_indices]
        days_diff = np.abs((cand_dates - ref_np).astype(np.float32))
        recency_weights = np.power(0.5, days_diff / self.half_life_days)
        scores = cand_sims * recency_weights

        if len(scores) > top_k:
            top_part = np.argpartition(scores, -top_k)[-top_k:]
            top_indices = top_part[np.argsort(-scores[top_part])]
        else:
            top_indices = np.argsort(-scores)

        best_orig_indices = candidate_indices[top_indices]

        results = []
        for idx_in_cands, orig_idx in zip(top_indices, best_orig_indices):
            raw_d = self.deck_dates[orig_idx]
            if isinstance(raw_d, date):
                d_date = raw_d
            else:
                d_date = date.fromisoformat(str(raw_d)[:10])

            deck_colors = (
                str(self.deck_colors[orig_idx])
                if len(self.deck_colors) > orig_idx
                else ""
            )
            deck_color_name = (
                str(self.deck_color_names[orig_idx])
                if len(self.deck_color_names) > orig_idx
                else ""
            )

            results.append(
                {
                    "deck_id": str(self.deck_ids[orig_idx]),
                    "player": str(self.deck_players[orig_idx]),
                    "archetype": str(self.deck_archetypes[orig_idx]),
                    "colors": deck_colors,
                    "color_name": deck_color_name,
                    "format": str(self.deck_formats[orig_idx]),
                    "date": d_date,
                    "raw_similarity": float(cand_sims[idx_in_cands]),
                    "recency_weight": float(recency_weights[idx_in_cands]),
                    "score": float(scores[idx_in_cands]),
                }
            )

        return results

    def _get_format_submatrix(self, format_name: str) -> tuple[csr_matrix, np.ndarray]:
        """Get or lazily cache the format-sliced TF-IDF submatrix and original indices."""
        if not hasattr(self, "_format_submatrices"):
            self._format_submatrices = {}
        if format_name not in self._format_submatrices:
            self._ensure_arrays()
            mask = self.deck_formats == format_name
            indices = np.where(mask)[0]
            if len(indices) == 0:
                submatrix = csr_matrix(
                    (0, self.tfidf_matrix.shape[1]), dtype=np.float32
                )
            else:
                submatrix = self.tfidf_matrix[indices]
            self._format_submatrices[format_name] = (submatrix, indices)
        return self._format_submatrices[format_name]

    def find_closest_classified(
        self,
        query_vec: csr_matrix,
        format_filter: str,
        known_archetypes: set[str] | None = None,
        min_similarity: float = 0.85,
        only_rule_classified: bool = True,
    ) -> tuple[str, str, float, str] | None:
        """Find the closest classified deck in the same format with raw cosine similarity >= min_similarity.

        Returns:
            (archetype_name, archetype_slug, similarity, deck_id) or None.
        """
        if self.tfidf_matrix is None or self.tfidf_matrix.shape[0] == 0:
            return None

        self._ensure_arrays()
        submatrix, orig_indices = self._get_format_submatrix(format_filter)
        if submatrix.shape[0] == 0:
            return None

        # Cosine similarity against format submatrix
        similarities = (submatrix @ query_vec.T).toarray().ravel()
        cand_mask = similarities >= min_similarity
        cand_local_indices = np.where(cand_mask)[0]
        if len(cand_local_indices) == 0:
            return None

        cand_orig_indices = orig_indices[cand_local_indices]
        cand_sims = similarities[cand_local_indices]

        # Filter candidates to only classified decks
        if known_archetypes is not None:
            classified_mask = np.fromiter(
                (
                    self.deck_archetypes[i] in known_archetypes
                    for i in cand_orig_indices
                ),
                dtype=bool,
                count=len(cand_orig_indices),
            )
            cand_orig_indices = cand_orig_indices[classified_mask]
            cand_sims = cand_sims[classified_mask]
            if len(cand_orig_indices) == 0:
                return None

        # Filter candidates to only rule-classified decks
        if only_rule_classified and len(self.deck_classification_methods) > 0:
            cand_methods = self.deck_classification_methods[cand_orig_indices]
            rule_mask = cand_methods == "rule"
            cand_orig_indices = cand_orig_indices[rule_mask]
            cand_sims = cand_sims[rule_mask]
            if len(cand_orig_indices) == 0:
                return None

        best_idx = np.argmax(cand_sims)
        best_sim = float(cand_sims[best_idx])
        best_orig_idx = cand_orig_indices[best_idx]
        best_arch = str(self.deck_archetypes[best_orig_idx])
        best_id = str(self.deck_ids[best_orig_idx])

        return best_arch, slugify_archetype(best_arch), best_sim, best_id


def build_knn_index(
    dest_path: Path | str | None = None,
    stdout: Any = None,
) -> tuple[Path, int, int, int]:
    """Build the global TF-IDF kNN index for all tournaments and save as compressed .npz.

    Returns:
        (saved_path, deck_count, vocab_size, non_zeros)
    """
    if dest_path is None:
        dest_path = getattr(
            settings,
            "KNN_INDEX_PATH",
            Path(settings.BASE_DIR) / "data" / "knn_index.npz",
        )
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if stdout:
        stdout.write("Fetching all tournament decks from database...")

    qs = (
        Deck.objects.select_related("tournament")
        .all()
        .order_by("tournament__date", "id")
    )
    (
        deck_ids,
        deck_formats,
        deck_players,
        deck_archetypes,
        deck_colors,
        deck_color_names,
        deck_classification_methods,
        deck_dates,
    ) = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    vocab_map: dict[str, int] = {}
    rows, cols, data = [], [], []

    for row_idx, d in enumerate(
        qs.values(
            "id",
            "format",
            "player",
            "archetype",
            "colors",
            "color_name",
            "classification_method",
            "is_auto_classified",
            "tournament__date",
            "mainboard",
            "sideboard",
        ).iterator(chunk_size=10000)
    ):
        deck_ids.append(d["id"])
        deck_formats.append(d["format"])
        deck_players.append(d["player"])
        deck_archetypes.append(d["archetype"])
        deck_colors.append(d.get("colors", "") or "")
        deck_color_names.append(d.get("color_name", "") or "")
        method = d.get("classification_method") or (
            "fallback" if d.get("is_auto_classified") else ""
        )
        deck_classification_methods.append(method)
        d_date = d["tournament__date"]
        deck_dates.append(
            d_date.isoformat() if hasattr(d_date, "isoformat") else str(d_date)
        )

        counts: dict[str, float] = {}
        for item in d.get("mainboard", []):
            c = normalize_card_name(item.get("card", ""))
            if c:
                counts[c] = counts.get(c, 0.0) + float(item.get("count", 1))

        for item in d.get("sideboard", []):
            c = normalize_card_name(item.get("card", ""))
            if c:
                counts[c] = counts.get(c, 0.0) + 0.5 * float(item.get("count", 1))

        for card, cnt in counts.items():
            idx = vocab_map.get(card)
            if idx is None:
                idx = len(vocab_map)
                vocab_map[card] = idx
            rows.append(row_idx)
            cols.append(idx)
            data.append(cnt)

        if stdout and (row_idx + 1) % 100000 == 0:
            stdout.write(f"Processed {row_idx + 1} decks...")

    n_decks = len(deck_ids)
    n_features = len(vocab_map)
    non_zeros = len(data)

    if stdout:
        stdout.write(
            f"Building sparse matrix for {n_decks} decks with {n_features} unique cards..."
        )

    term_matrix = csr_matrix(
        (data, (rows, cols)), shape=(n_decks, n_features), dtype=np.float32
    )

    transformer = TfidfTransformer(norm="l2", sublinear_tf=True)
    tfidf_matrix = transformer.fit_transform(term_matrix)

    inv_vocab = [None] * n_features
    for card, idx in vocab_map.items():
        inv_vocab[idx] = card

    if stdout:
        stdout.write(f"Compressing and saving to {dest}...")

    np.savez_compressed(
        dest,
        data=tfidf_matrix.data,
        indices=tfidf_matrix.indices,
        indptr=tfidf_matrix.indptr,
        shape=tfidf_matrix.shape,
        idf=transformer.idf_,
        deck_ids=np.array(deck_ids),
        deck_formats=np.array(deck_formats),
        deck_players=np.array(deck_players),
        deck_archetypes=np.array(deck_archetypes),
        deck_colors=np.array(deck_colors),
        deck_color_names=np.array(deck_color_names),
        deck_classification_methods=np.array(deck_classification_methods),
        deck_dates=np.array(deck_dates),
        vocab=np.array(inv_vocab),
    )

    loaded = DeckKNNIndex.load(dest)
    set_global_knn_index(loaded)

    return dest, n_decks, n_features, non_zeros


_GLOBAL_INDEX: "DeckKNNIndex | None" = None
_GLOBAL_INDEX_LOADED: bool = False
_GLOBAL_INDEX_MTIME: float = 0.0
_CARD_IDF_MAP: dict[str, float] | None = None
_CARD_IDF_MTIME: float = 0.0


def get_card_idf_map() -> dict[str, float]:
    """Return a lightweight mapping of normalized card name -> IDF weight.

    If the full global kNN index is already loaded in memory, extracts IDFs from it.
    Otherwise reads only the 'vocab' and 'idf' arrays (~34 KB) from knn_index.npz
    without loading the ~280 MB sparse TF-IDF matrix into worker memory.
    """
    global _CARD_IDF_MAP, _CARD_IDF_MTIME

    if _GLOBAL_INDEX_LOADED:
        if (
            _GLOBAL_INDEX is not None
            and _GLOBAL_INDEX.transformer is not None
            and hasattr(_GLOBAL_INDEX.transformer, "idf_")
            and _GLOBAL_INDEX.vocab
        ):
            idf_arr = _GLOBAL_INDEX.transformer.idf_
            return {
                str(card): float(idf_arr[idx])
                for card, idx in _GLOBAL_INDEX.vocab.items()
            }
        return {}

    index_path = getattr(
        settings, "KNN_INDEX_PATH", Path(settings.BASE_DIR) / "data" / "knn_index.npz"
    )
    p = Path(index_path)
    if not p.exists():
        return {}

    try:
        current_mtime = p.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if _CARD_IDF_MAP is not None and current_mtime <= _CARD_IDF_MTIME:
        return _CARD_IDF_MAP

    try:
        with np.load(p, allow_pickle=False) as f:
            vocab = f["vocab"]
            idf = f["idf"]
            _CARD_IDF_MAP = {str(card): float(val) for card, val in zip(vocab, idf)}
            _CARD_IDF_MTIME = current_mtime
            return _CARD_IDF_MAP
    except Exception as exc:
        logger.debug("Failed to load lightweight IDF map from %s: %s", p, exc)
        return {}


def get_global_knn_index(force_reload: bool = False) -> DeckKNNIndex | None:
    """Retrieve the in-memory global kNN index singleton, loading it from disk if needed."""
    global _GLOBAL_INDEX, _GLOBAL_INDEX_LOADED, _GLOBAL_INDEX_MTIME

    index_path = getattr(
        settings, "KNN_INDEX_PATH", Path(settings.BASE_DIR) / "data" / "knn_index.npz"
    )
    p = Path(index_path)

    # If an in-memory index is already loaded and we are not forcing a reload,
    # return it unless the file on disk has been updated.
    if _GLOBAL_INDEX_LOADED and not force_reload:
        if _GLOBAL_INDEX is not None:
            try:
                if p.exists() and p.stat().st_mtime > _GLOBAL_INDEX_MTIME:
                    pass  # file on disk is newer, proceed to reload below
                else:
                    return _GLOBAL_INDEX
            except OSError:
                return _GLOBAL_INDEX
        else:
            # Index was previously checked and not found; return None unless file now exists
            if not p.exists():
                return None

    if not p.exists():
        if not _GLOBAL_INDEX_LOADED or force_reload:
            msg = (
                f"kNN index file not found at '{p}'. Deck similarity neighbors will not be available. "
                "Run 'uv run modometa build_knn' to generate it."
            )
            logger.warning(msg)
            warnings.warn(msg, UserWarning, stacklevel=2)
            _GLOBAL_INDEX_LOADED = True
            _GLOBAL_INDEX = None
            _GLOBAL_INDEX_MTIME = 0.0
        return None

    try:
        current_mtime = p.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    try:
        logger.info("Loading kNN index from %s...", p)
        _GLOBAL_INDEX = DeckKNNIndex.load(p)
        _GLOBAL_INDEX_LOADED = True
        _GLOBAL_INDEX_MTIME = current_mtime
        return _GLOBAL_INDEX
    except Exception as exc:
        logger.error("Failed to load kNN index from %s: %s", p, exc)
        _GLOBAL_INDEX_LOADED = True
        _GLOBAL_INDEX = None
        _GLOBAL_INDEX_MTIME = 0.0
        return None


def set_global_knn_index(index: DeckKNNIndex | None, loaded: bool = True) -> None:
    """Set the global in-memory kNN index singleton."""
    global _GLOBAL_INDEX, _GLOBAL_INDEX_LOADED, _GLOBAL_INDEX_MTIME, _CARD_IDF_MAP
    _GLOBAL_INDEX = index
    _GLOBAL_INDEX_LOADED = loaded
    _CARD_IDF_MAP = None
    if index is not None:
        index_path = getattr(
            settings,
            "KNN_INDEX_PATH",
            Path(settings.BASE_DIR) / "data" / "knn_index.npz",
        )
        p = Path(index_path)
        try:
            _GLOBAL_INDEX_MTIME = p.stat().st_mtime if p.exists() else 0.0
        except OSError:
            _GLOBAL_INDEX_MTIME = 0.0
    else:
        _GLOBAL_INDEX_MTIME = 0.0
