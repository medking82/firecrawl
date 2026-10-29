const {test}=require('node:test'),assert=require('node:assert/strict');
const {publicIP}=require('./manager.cjs');
test('egress denies localhost, private, metadata, special-use and IPv6',()=>{
 for(const ip of ['127.0.0.1','10.0.0.1','172.16.0.1','172.31.255.255','192.168.1.1','169.254.169.254','100.64.0.1','100.127.1.1','0.0.0.0','224.0.0.1','198.18.0.1','198.51.100.1','203.0.113.1','::1','::ffff:127.0.0.1','fc00::1','192.0.0.1'])assert.equal(publicIP(ip),false,ip);
 for(const ip of ['1.1.1.1','8.8.8.8','93.184.215.14','172.15.1.1','172.32.1.1'])assert.equal(publicIP(ip),true,ip);
});
