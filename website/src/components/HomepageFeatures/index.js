import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

const FeatureList = [
  {
    title: 'Drop-in for os.walk',
    description: (
      <>
        Same signature, same yield order, and in-place <code>dirnames</code>{' '}
        pruning works exactly as it does in the standard library — a pruned
        subtree is skipped with a seek rather than read and discarded.
      </>
    ),
  },
  {
    title: 'Built for one sequential pass',
    description: (
      <>
        Each directory&apos;s children sit in one contiguous run of rows, laid
        out depth-first. A full walk reads the index strictly forward — zero
        backward seeks — so readahead works even when the index is far larger
        than RAM.
      </>
    ),
  },
  {
    title: 'Flat memory, free rollups',
    description: (
      <>
        A walk holds only the directories on the current path: kilobytes, no
        matter how large the tree. Sizes and file counts are rolled up during
        the scan, so <code>du()</code> is a single lookup.
      </>
    ),
  },
];

function Feature({title, description}) {
  return (
    <div className={clsx('col col--4')}>
      <div className="padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures() {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
