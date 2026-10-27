/** Route A — group-grade read path: DRX->UI scaling + group discovery.
 * Validates scaleParam against DRX-VALUE-SCALING.md and listGroupNames against the
 * project.xml <Sm2Group><Name> shape. The decoder itself is covered by
 * group-grade-calibration.test.mjs / drx-value-fidelity.test.mjs. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { scaleParam, listGroupNames, groupSegment, groupBodies } from '../server/group-grade-read.mjs';

test('scaleParam matches DRX-VALUE-SCALING.md', () => {
  assert.equal(scaleParam('lift.r', 0.5), 0.25); // lift /2
  assert.equal(scaleParam('gamma.master', 1.0), 0.25); // gamma /4
  assert.equal(scaleParam('gain.b', 1.5), 1.5); // gain 1:1
  assert.equal(scaleParam('offset.r', 0.004), 10); // offset *2500
  assert.equal(scaleParam('saturation.primary', 1.5), 150); // sat *100 (%)
  assert.equal(scaleParam('contrast', 1.5), 150); // contrast *100
  assert.equal(scaleParam('logHighlight.r', -0.671), -0.671); // log wheel raw
});

test('listGroupNames pulls Sm2Group names, de-duped', () => {
  const xml = [
    '<Sm2GroupList><Element><Sm2Group><FieldsBlob/><Name>Host</Name></Sm2Group></Element>',
    '<Element><Sm2Group><FieldsBlob/><Name>Guest</Name></Sm2Group></Element>',
    '<Element><Sm2Group><FieldsBlob/><Name>Host</Name></Sm2Group></Element></Sm2GroupList>',
  ].join('');
  assert.deepEqual(listGroupNames(xml), ['Host', 'Guest']);
});

test('group names are read and matched as text, not as their xml escaping', () => {
  // project.xml stores `&` as `&amp;`. The listing reported `Night &amp; Day`, and a
  // caller asking for `Night & Day` got "group not found". Apostrophes may be stored
  // either way, so both spellings resolve.
  const xml = [
    '<Sm2GroupList><Element><Sm2Group><FieldsBlob/><Name>Night &amp; Day</Name><Body>aa</Body></Sm2Group></Element>',
    "<Element><Sm2Group><FieldsBlob/><Name>Sam's</Name><Body>bb</Body></Sm2Group></Element>",
    '<Element><Sm2Group><FieldsBlob/><Name>Ann&apos;s</Name><Body>cc</Body></Sm2Group></Element></Sm2GroupList>',
  ].join('');
  assert.deepEqual(listGroupNames(xml), ['Night & Day', "Sam's", "Ann's"]);
  assert.match(groupSegment(xml, 'Night & Day'), /<Body>aa<\/Body>/);
  assert.match(groupSegment(xml, "Sam's"), /<Body>bb<\/Body>/);
  assert.match(groupSegment(xml, "Ann's"), /<Body>cc<\/Body>/);
  assert.equal(groupSegment(xml, 'Night'), null);
});

test('groupSegment matches the group name, not an earlier element with the same name', () => {
  const xml = [
    '<Sm2Group><FieldsBlob/><Name>Host</Name><Body>11</Body></Sm2Group>',
    '<Sm2MpVideoClip><Name>Guest</Name></Sm2MpVideoClip>',
    '<Sm2Group><FieldsBlob/><Name>Guest</Name><Body>22</Body></Sm2Group>',
  ].join('');
  // The old lookup spanned from the first group to the second and returned both bodies.
  assert.deepEqual(groupBodies(groupSegment(xml, 'Guest')), ['22']);
});
