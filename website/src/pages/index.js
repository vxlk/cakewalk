import clsx from 'clsx';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import CodeBlock from '@theme/CodeBlock';
import HomepageFeatures from '@site/src/components/HomepageFeatures';

import Heading from '@theme/Heading';
import styles from './index.module.css';

const SAMPLE = `import cakewalk

cakewalk.update_cache("D:\\\\share")     # sweep the filesystem once

for root, dirs, files in cakewalk.walk("D:\\\\share"):
    ...                                 # reads the index, not the disk

# ...or ask the index directly, and skip building 5 million strings
conn = cakewalk.connect()
lo, hi = cakewalk.subtree_range("D:\\\\share")
conn.execute("SELECT sum(size) FROM fs_nodes "
             "WHERE id BETWEEN ? AND ? AND is_dir = 0", (lo, hi))`;

function HomepageHeader() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className={clsx('hero hero--primary', styles.heroBanner)}>
      <div className="container">
        <Heading as="h1" className="hero__title">
          {siteConfig.title}
        </Heading>
        <p className="hero__subtitle">{siteConfig.tagline}</p>
        <div className={styles.buttons}>
          <Link className="button button--secondary button--lg" to="/docs">
            Read the docs
          </Link>
        </div>
      </div>
    </header>
  );
}

export default function Home() {
  return (
    <Layout
      title="A filesystem index for Python"
      description="A SQLite index of your filesystem: a drop-in os.walk, and a database you can query directly.">
      <HomepageHeader />
      <main>
        <section className="container margin-top--lg">
          <div className="row">
            <div className="col col--8 col--offset-2">
              <CodeBlock language="python">{SAMPLE}</CodeBlock>
              <p>
                cakewalk exists for one situation: repeatedly asking questions
                about a very large tree — a multi-terabyte share, a network
                mount, a spinning disk — where the filesystem itself is the
                bottleneck. Sweep it once, then read the index instead of the
                disk.
              </p>
              <p>
                <code>walk()</code> is the compatibility layer, and it is about
                30x faster than <code>os.walk</code>. The index underneath it is
                an ordinary SQLite database where a subtree is a contiguous id
                range, and{' '}
                <Link to="/docs/sql">querying it directly</Link> is 16x to 693x
                faster — because an aggregate never builds a Python object per
                file.
              </p>
              <p>
                It is not magic, and the{' '}
                <Link to="/docs/performance">performance page</Link> is explicit
                about which numbers were measured and which are extrapolation.
                If you walk a directory once, the scan costs more than the walk
                it saves — read{' '}
                <Link to="/docs/limitations">the limitations</Link> first.
              </p>
            </div>
          </div>
        </section>
        <HomepageFeatures />
      </main>
    </Layout>
  );
}
