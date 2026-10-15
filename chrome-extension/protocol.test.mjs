import {test} from 'node:test';
import assert from 'node:assert/strict';
import {shareableUrl,linkPayload} from './protocol.js';

test('user-selected tab preserves URL query and fragment',()=>{
  assert.deepEqual(linkPayload({url:'https://example.com/a?q=1#part',title:'Страница'},'request-one'),
    {url:'https://example.com/a?q=1#part',title:'Страница',target:'phone',requestId:'request-one'});
});
test('scripts, files, credentials and line breaks cannot become an open-tab request',()=>{
  for(const value of ['javascript:alert(1)','file:///private','chrome://settings','https://u:p@example.com','https://example.com/\nother'])
    assert.throws(()=>shareableUrl(value));
});
test('website-controlled title is bounded plain text',()=>{
  const payload=linkPayload({url:'https://example.com',title:'<img onerror=evil>'.repeat(100)},'two');
  assert.equal(payload.title.length,500);
  assert.equal(payload.target,'phone');
});
