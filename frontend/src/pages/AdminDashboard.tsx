import React, { useState, useEffect } from 'react';

// Define the type for a single customer based on our Pydantic schema
interface Customer {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  phone_number: string | null;
  address: string | null;
  whatsapp_number: string | null;
  facebook_handle: string | null;
  instagram_handle: string | null;
  twitter_handle: string | null;
  created_at: string;
  updated_at: string;
}

const API_BASE_URL = '/api/v1';

const AdminDashboard = () => {
  const [customers, setCustomers] = useState<Customer[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [email, setEmail] = useState('');
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [phone, setPhone] = useState('');
  const [address, setAddress] = useState('');
  const [whatsapp, setWhatsapp] = useState('');
  const [facebook, setFacebook] = useState('');
  const [instagram, setInstagram] = useState('');
  const [twitter, setTwitter] = useState('');

  // --- Style Objects ---
  const styleSheet = {
    container: { fontFamily: 'Arial, sans-serif', padding: '20px' },
    header: { borderBottom: '2px solid #eee', paddingBottom: '10px' },
    section: { marginTop: '30px' },
    form: { background: '#f9f9f9', padding: '15px', borderRadius: '8px', border: '1px solid #eee', display: 'flex', flexDirection: 'column' as 'column', gap: '10px', maxWidth: '400px' },
    input: { padding: '8px', borderRadius: '4px', border: '1px solid #ccc' },
    button: { padding: '10px 15px', borderRadius: '4px', border: 'none', background: '#007bff', color: 'white', cursor: 'pointer' },
    table: { width: '100%', borderCollapse: 'collapse' as 'collapse', marginTop: '20px', tableLayout: 'fixed' as 'fixed' },
    th: { borderBottom: '1px solid #ddd', padding: '12px', textAlign: 'left' as 'left', background: '#f2f2f2', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    td: { borderBottom: '1px solid #ddd', padding: '12px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    deleteButton: { padding: '5px 10px', borderRadius: '4px', border: 'none', background: '#dc3545', color: 'white', cursor: 'pointer' },
    error: { color: 'red', marginTop: '10px' },
  };
  
  // --- API Functions ---

  // Fetch all customers
  const fetchCustomers = async () => {
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/customers/`);
      if (!response.ok) {
        const errorData = await response.text();
        throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
      }
      const data = await response.json();
      setCustomers(data);
    } catch (e) {
      if (e instanceof Error) {
        setError(`Failed to fetch customers: ${e.message}`);
      }
    }
  };

  // Effect to fetch customers on component mount
  useEffect(() => {
    fetchCustomers();
  }, []);

  // Handle form submission to create a new customer
  const handleCreateCustomer = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/customers/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          email,
          first_name: firstName,
          last_name: lastName,
          phone_number: phone || null,
          address: address || null,
          whatsapp_number: whatsapp || null,
          facebook_handle: facebook || null,
          instagram_handle: instagram || null,
          twitter_handle: twitter || null,
        }),
      });
      if (!response.ok) {
        const errorData = await response.text();
        throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
      }
      // Reset form and refetch customers
      setEmail('');
      setFirstName('');
      setLastName('');
      setPhone('');
      setAddress('');
      setWhatsapp('');
      setFacebook('');
      setInstagram('');
      setTwitter('');
      fetchCustomers();
    } catch (e) {
       if (e instanceof Error) {
        setError(`Failed to create customer: ${e.message}`);
      }
    }
  };

  // Handle deleting a customer
  const handleDeleteCustomer = async (id: string) => {
    if (window.confirm('Are you sure you want to delete this customer?')) {
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/customers/${id}`, {
          method: 'DELETE',
        });
        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP error! status: ${response.status} - ${errorData}`);
        }
        fetchCustomers();
      } catch (e) {
        if (e instanceof Error) {
          setError(`Failed to delete customer: ${e.message}`);
        }
      }
    }
  };

  return (
    <div style={styleSheet.container}>
      <h1 style={styleSheet.header}>Administrative Dashboard</h1>
      
      {error && <p style={styleSheet.error}>{error}</p>}

      {/* --- Customer Management Section --- */}
      <div style={styleSheet.section}>
        <h2>Customer Management</h2>
        
        {/* Create Customer Form */}
        <form onSubmit={handleCreateCustomer} style={styleSheet.form}>
          <h3>Add New Customer</h3>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" required style={styleSheet.input} />
          <input type="text" value={firstName} onChange={(e) => setFirstName(e.target.value)} placeholder="First Name" required style={styleSheet.input} />
          <input type="text" value={lastName} onChange={(e) => setLastName(e.target.value)} placeholder="Last Name" required style={styleSheet.input} />
          <input type="text" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="Phone (Optional)" style={styleSheet.input} />
          <input type="text" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="Address (Optional)" style={styleSheet.input} />
          <input type="text" value={whatsapp} onChange={(e) => setWhatsapp(e.target.value)} placeholder="WhatsApp (Optional)" style={styleSheet.input} />
          <input type="text" value={facebook} onChange={(e) => setFacebook(e.target.value)} placeholder="Facebook (Optional)" style={styleSheet.input} />
          <input type="text" value={instagram} onChange={(e) => setInstagram(e.target.value)} placeholder="Instagram (Optional)" style={styleSheet.input} />
          <input type="text" value={twitter} onChange={(e) => setTwitter(e.target.value)} placeholder="Twitter (Optional)" style={styleSheet.input} />
          <button type="submit" style={styleSheet.button}>Add Customer</button>
        </form>

        {/* Customer List Table */}
        <table style={styleSheet.table}>
          <thead>
            <tr>
              <th style={styleSheet.th}>ID</th>
              <th style={styleSheet.th}>Email</th>
              <th style={styleSheet.th}>First Name</th>
              <th style={styleSheet.th}>Last Name</th>
              <th style={styleSheet.th}>Phone Number</th>
              <th style={styleSheet.th}>Address</th>
              <th style={styleSheet.th}>WhatsApp</th>
              <th style={styleSheet.th}>Facebook</th>
              <th style={styleSheet.th}>Instagram</th>
              <th style={styleSheet.th}>Twitter</th>
              <th style={styleSheet.th}>Created</th>
              <th style={styleSheet.th}>Updated</th>
              <th style={styleSheet.th}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {customers.map((customer) => (
              <tr key={customer.id}>
                <td style={styleSheet.td}>{customer.id}</td>
                <td style={styleSheet.td}>{customer.email}</td>
                <td style={styleSheet.td}>{customer.first_name}</td>
                <td style={styleSheet.td}>{customer.last_name}</td>
                <td style={styleSheet.td}>{customer.phone_number}</td>
                <td style={styleSheet.td}>{customer.address}</td>
                <td style={styleSheet.td}>{customer.whatsapp_number}</td>
                <td style={styleSheet.td}>{customer.facebook_handle}</td>
                <td style={styleSheet.td}>{customer.instagram_handle}</td>
                <td style={styleSheet.td}>{customer.twitter_handle}</td>
                <td style={styleSheet.td}>{new Date(customer.created_at).toLocaleString()}</td>
                <td style={styleSheet.td}>{new Date(customer.updated_at).toLocaleString()}</td>
                <td style={styleSheet.td}>
                  <button onClick={() => handleDeleteCustomer(customer.id)} style={styleSheet.deleteButton}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* --- Placeholder Sections --- */}
      <div style={styleSheet.section}><h2>Brand Management</h2></div>
      <div style={styleSheet.section}><h2>Product Catalogue</h2></div>
      <div style={styleSheet.section}><h2>Promotion Management</h2></div>
    </div>
  );
};

export default AdminDashboard;