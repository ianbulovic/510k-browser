import { loadMeta } from "../data";
import { useAsync } from "../hooks";
import { Pending } from "./common";

export function About() {
  const meta = useAsync(loadMeta, []);
  return (
    <section class="page prose">
      <h1>About the data</h1>
      <p>
        This site isn't affiliated with the FDA. Predicate links are extracted automatically from
        510(k) summaries and can be wrong. Check the FDA record before relying on any of them.
      </p>

      <h2>Where it comes from</h2>
      <p>
        Device records come from the{" "}
        <a href="https://open.fda.gov/apis/device/510k/">openFDA 510(k) dataset</a> and product
        codes from the{" "}
        <a href="https://open.fda.gov/apis/device/classification/">
          openFDA classification dataset
        </a>
        . Neither includes predicates: those are named only in each clearance's 510(k) summary, a
        free-form PDF.
      </p>
      <p>
        The predicate links on this site currently come from{" "}
        <a href="https://github.com/wcedmisten/fda-510k-analysis">wcedmisten/fda-510k-analysis</a>,
        which read about 86,000 summaries with text extraction, OCR and manual review. Its{" "}
        <a href="https://wcedmisten-fyi.pages.dev/post/medical-device-analysis">write-up</a>{" "}
        explains the method. Links entered there by hand are marked as such.
      </p>

      <h2>Known gaps</h2>
      <ul>
        <li>
          Clearances with a 510(k) <em>statement</em> instead of a summary don't make their
          predicates public. Most clearances before the late 1990s are like this.
        </li>
        <li>
          Some summaries describe a predicate by name without its number. Those links are missing.
        </li>
        <li>
          A link drawn in red points to a predicate the FDA decided after the citing device was
          submitted. That usually means the number was misread.
        </li>
        <li>Predicates that are De Novo requests or PMA approvals aren't linked yet.</li>
      </ul>

      <h2>Report a wrong link</h2>
      <p>
        Open an issue on{" "}
        <a href="https://github.com/Machine-Learning-for-Medical-Language/510k-browser/issues">
          GitHub
        </a>{" "}
        with the two submission numbers and what the summary says.
      </p>

      <Pending value={meta} label="build details">
        {(m) => (
          <>
            <h2>This build</h2>
            <dl class="details">
              <div>
                <dt>Built</dt>
                <dd>{m.built_at}</dd>
              </div>
              <div>
                <dt>openFDA 510(k) export</dt>
                <dd>{m.sources.devices_export_date ?? "unknown"}</dd>
              </div>
              <div>
                <dt>Pipeline version</dt>
                <dd>{m.pipeline_version}</dd>
              </div>
            </dl>
          </>
        )}
      </Pending>
    </section>
  );
}
